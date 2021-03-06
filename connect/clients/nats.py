"""
nats.py
NATS message subscribers and message handlers
"""
import json
import logging
import connect.workflows.core as core
import ssl
from asyncio import get_running_loop
from nats.aio.client import Client as NatsClient, Msg
from connect.clients.kafka import get_kafka_producer, KafkaCallback
from connect.config import (
    get_settings,
    get_ssl_context,
    nats_sync_subject,
    kafka_sync_topic,
)
from connect.support.encoding import decode_to_dict
from typing import Callable, List, Optional
import os


logger = logging.getLogger(__name__)
nats_client = None
nats_clients = []
timing_metrics = {}


async def create_nats_subscribers():
    """
    Create NATS subscribers.  Add additional subscribers as needed.
    """
    await start_sync_event_subscribers()
    await start_timing_subscriber()


async def start_sync_event_subscribers():
    """
    Create a NATS subscriber for 'nats_sync_subject' for the local NATS server/cluster and
    for each NATS server defined by 'nats_sync_subscribers' in config.py.
    """
    settings = get_settings()

    # subscribe to nats_sync_subject from the local NATS server or cluster
    client = await get_nats_client()
    await subscribe(
        client,
        nats_sync_subject,
        nats_sync_event_handler,
        "".join(settings.nats_servers),
    )

    # subscribe to nats_sync_subject from any additional NATS servers
    for server in settings.nats_sync_subscribers:
        client = await create_nats_client(server)
        await subscribe(client, nats_sync_subject, nats_sync_event_handler, server)


async def start_timing_subscriber():
    """
    Create a NATS subscriber for the NATS subject TIMING at the local NATS server/cluster.
    """
    settings = get_settings()

    # subscribe to TIMING.* from the local NATS server or cluster
    client = await get_nats_client()
    await subscribe(
        client,
        "TIMING",
        nats_timing_event_handler,
        "".join(settings.nats_servers),
    )


async def subscribe(client: NatsClient, subject: str, callback: Callable, servers: str):
    """
    Subscribe a NATS client to a subject.

    :param client: a connected NATS client
    :param subject: the NATS subject to subscribe to
    :param callback: the callback to call when a message is received on the subscription
    """
    await client.subscribe(subject, cb=callback)
    nats_clients.append(client)
    logger.debug(f"Subscribed {servers} to NATS subject {subject}")


async def nats_sync_event_handler(msg: Msg):
    """
    Callback for NATS 'nats_sync_subject' messages
    """
    subject = msg.subject
    reply = msg.reply
    data = msg.data.decode()
    logger.trace(f"nats_sync_event_handler: received a message on {subject} {reply}")

    # if the message is from our local LFH, don't store in kafka
    message = json.loads(data)
    if get_settings().connect_lfh_id == message["lfh_id"]:
        logger.trace(
            "nats_sync_event_handler: detected local LFH message, not storing in kafka",
        )
        return

    # store the message in kafka
    kafka_producer = get_kafka_producer()
    kafka_cb = KafkaCallback()
    await kafka_producer.produce_with_callback(
        kafka_sync_topic, data, on_delivery=kafka_cb.get_kafka_result
    )
    logger.trace(
        f"nats_sync_event_handler: stored msg in kafka topic {kafka_sync_topic} at {kafka_cb.kafka_result}",
    )

    # process the message into the local store
    settings = get_settings()
    msg_data = decode_to_dict(message["data"])
    workflow = core.CoreWorkflow(
        message=msg_data,
        origin_url=message["consuming_endpoint_url"],
        certificate_verify=settings.certificate_verify,
        lfh_id=message["lfh_id"],
        data_format=message["data_format"],
        transmit_server=None,
        do_sync=False,
    )

    result = await workflow.run(None)
    location = result["data_record_location"]
    logger.trace(
        f"nats_sync_event_handler: replayed nats sync message, data record location = {location}",
    )


def nats_timing_event_handler(msg: Msg):
    """
    Callback for NATS TIMING messages - calculates the average run time for any function timed with @timer.
    """
    data = msg.data.decode()

    message = json.loads(data)
    function_name = message["function"]
    global timing_metrics
    if function_name not in timing_metrics:
        timing_metrics[function_name] = {"total": 0.0, "count": 0, "average": 0.0}

    metric = timing_metrics[function_name]
    metric["total"] += message["elapsed_time"]
    metric["count"] += 1
    metric["average"] = metric["total"] / metric["count"]
    logger.trace(
        f"nats_timing_event_handler: {function_name}() average elapsed time = {metric['average']:.8f}s",
    )


async def stop_nats_clients():
    """
    Gracefully stop all NATS clients prior to shutdown, including
    unsubscribing from all subscriptions.
    """
    for client in nats_clients:
        await client.close()


async def get_nats_client() -> Optional[NatsClient]:
    """
    Create or return a NATS client connected to the local
    NATS server or cluster defined by 'nats_servers' in config.py.

    :return: a connected NATS client instance
    """
    global nats_client

    if not nats_client:
        settings = get_settings()
        nats_client = await create_nats_client(settings.nats_servers)
        nats_clients.append(nats_client)

    return nats_client


async def create_nats_client(servers: List[str]) -> Optional[NatsClient]:
    """
    Create a NATS client for any NATS server or NATS cluster configured to accept this installation's NKey.

    :param servers: List of one or more NATS servers.  If multiple servers are
    provided, they should be in the same NATS cluster.
    :return: a connected NATS client instance
    """
    settings = get_settings()

    client = NatsClient()
    await client.connect(
        servers=servers,
        nkeys_seed=os.path.join(
            settings.connect_config_directory, settings.nats_nk_file
        ),
        loop=get_running_loop(),
        tls=get_ssl_context(ssl.Purpose.SERVER_AUTH),
        allow_reconnect=settings.nats_allow_reconnect,
        max_reconnect_attempts=settings.nats_max_reconnect_attempts,
    )
    logger.info("Created NATS client")
    logger.debug(f"Created NATS client for servers = {servers}")

    return client


async def get_client_status() -> Optional[str]:
    """
    Check to see if the default NATS client is connected.

    :return: CONNECTED if client is connected, CONNECTING if reconnecting, NOT_CONNECTED otherwise
    """
    client = await get_nats_client()

    if client.is_connected:
        return "CONNECTED"
    elif client.is_reconnecting:
        return "CONNECTING"
    else:
        return "NOT_CONNECTED"
