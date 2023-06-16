"""Demo peer to peer service: handle data message, video call, based on peerjs/ext/http-proxy.py """
# import argparse
import os
import asyncio
import sys
import json
import yaml
import aiohttp
from typing import Any
from pathlib import Path
from loguru import logger

# from aiortc import RTCIceCandidate, RTCSessionDescription
from peerjs.peer import Peer, PeerOptions
from peerjs.peerroom import PeerRoom
from peerjs.util import util, default_ice_servers
from peerjs.enums import ConnectionEventType, PeerEventType
from aiortc.rtcconfiguration import RTCConfiguration, RTCIceServer

print(sys.version)

peer = None
savedPeerId = None

relayScreen = None
webScreen = None

# persisted config dict

AMBIANIC_PNP_HOST = 'localhost' #'www.web-rtc.rectelework.com'
AMBIANIC_PNP_PORT = 9001 #35666
AMBIANIC_PNP_SECURE = False #True

DEFAULT_LOG_LEVEL = 'INFO'

config = {
    'signaling_server': AMBIANIC_PNP_HOST,
    'port':   AMBIANIC_PNP_PORT,
    'secure': AMBIANIC_PNP_SECURE,
    'ice_servers': [], #default_ice_servers,
    'log_level': "INFO",
}

PEERID_FILE = '.peerjsrc'
if os.environ.get("PEERJS_PEERID_FILE"):
    PEERID_FILE = os.environ.get("PEERJS_PEERID_FILE")

CONFIG_FILE = 'peerjs-config.yaml'
if os.environ.get("PEERJS_CONFIG_FILE"):
    CONFIG_FILE = os.environ.get("PEERJS_CONFIG_FILE")

time_start = None
peerConnectionStatus = None
discoveryLoop = None
# aiohttp session reusable throghout the http proxy lifecycle
http_session = None
# flags when user requests shutdown
# via CTRL+C or another system signal
_is_shutting_down: bool = False


# async def _consume_signaling(pc, signaling):
#     while True:
#         obj = await signaling.receive()
#         if isinstance(obj, RTCSessionDescription):
#             await pc.setRemoteDescription(obj)
#             if obj.type == "offer":
#                 # send answer
#                 await pc.setLocalDescription(await pc.createAnswer())
#                 await signaling.send(pc.localDescription)
#         elif isinstance(obj, RTCIceCandidate):
#             pc.addIceCandidate(obj)
#         elif obj is None:
#             print("Exiting")
#             break



async def join_peer_room(peer=None):
    """Join a peer room with other local peers."""
    # first try to find the remote peer ID in the same room
    myRoom = PeerRoom(peer)
    logger.debug('Fetching room members...')
    peerIds = await myRoom.getRoomMembers()
    logger.info('myRoom members {}', peerIds)


def _savePeerId(peerId=None):
    assert peerId
    global savedPeerId
    savedPeerId = peerId
    with open(PEERID_FILE, 'w') as outfile:
        json.dump({'peerId': peerId}, outfile)


def _loadPeerId():
    """Load and reuse saved peer ID if there is one."""
    global savedPeerId
    conf_file = Path(PEERID_FILE)
    if conf_file.exists():
        conf = {}
        with conf_file.open() as infile:
            conf = yaml.load(infile, Loader=yaml.SafeLoader)
        if conf is not None:
            savedPeerId = conf.get('peerId', None)
        else:
            savedPeerId = None

def _loadConfig():
    logger.info('Loading configuration')
    global config
    conf_file = Path(CONFIG_FILE)
    exists = conf_file.exists()
    if exists:
        logger.info(f'Loading config from: {conf_file}')
        with conf_file.open() as infile:
            config = yaml.load(infile, Loader=yaml.SafeLoader)
    else:
        logger.info(f'Config file not found: {conf_file}')
    # Set defaults
    if config is None:
        config = {}
    if "signaling_server" not in config.keys():
        config["signaling_server"] = AMBIANIC_PNP_HOST
    if "port" not in config.keys():
        config["port"] = AMBIANIC_PNP_PORT
    if "secure" not in config.keys():
        config["secure"] = AMBIANIC_PNP_SECURE
    if "ice_servers" not in config.keys():
        config["ice_servers"] = default_ice_servers

    return exists


def _saveConfig():
    global config
    cfg1 = config.copy()
    if 'peerId' in cfg1.keys():
        del cfg1["peerId"]
    with open(CONFIG_FILE, 'w') as outfile:
        yaml.dump(cfg1, outfile)


async def _pong(peer_connection=None):
    """Respond to client ping."""
    response_header = {
        'status': 200,
    }
    header_as_json = json.dumps(response_header)
    logger.debug('sending keepalive pong back to remote peer')
    await peer_connection.send(header_as_json)
    await peer_connection.send('pong')


async def _ping(peer_connection=None, stop_flag=None):
    while not stop_flag.is_set():
        # send HTTP 202 Accepted status code to inform
        # client that we are still waiting on the http
        # server to complete its response
        ping_as_json = json.dumps({'status': 202})
        await peer_connection.send(ping_as_json)
        logger.info('webrtc peer: http proxy response to client ping. '
                 'Keeping datachannel alive.')
        await asyncio.sleep(1)

def _set_peer_events(peer=None):
    assert peer
    global savedPeerId

    @peer.on(PeerEventType.Call)
    async def pc_call(remoteMediaConn):
        global relayScreen, webScreen
        logger.info('pc_call remoteMediaConn:')
        # Get stream by capture screen
        audio2, video2 = util.create_local_tracks(
            None,
            decode=None,
            source="desktop",
            format="gdigrab",
            options={"framerate": "5","video_size": "1366x768", "rtbufsize": "100M", "probesize": "100M"},
            relay=relayScreen,
            player=webScreen
        )
        await remoteMediaConn.answer(video2)

    @peer.on(PeerEventType.Open)
    async def peer_open(id):
        logger.info('Peer signaling connection open: {}', id )
        global savedPeerId
        # Workaround for peer.reconnect deleting previous id
        if peer.id is None:
            logger.info('pnpService: Received null id from peer open')
            peer.id = savedPeerId
        else:
            if savedPeerId != peer.id:
                logger.info(
                    'PNP Service returned new peerId. Old {}, New {}',
                    savedPeerId,
                    peer.id
                )
            _savePeerId(peer.id)
        logger.info('savedPeerId: {}', peer.id)

    @peer.on(PeerEventType.Disconnected)
    async def peer_disconnected(peerId):
        global savedPeerId
        logger.info('Peer {} disconnected from server.', peerId)
        # Workaround for peer.reconnect deleting previous id
        if not peer.id:
            logger.debug('BUG WORKAROUND: Peer lost ID. '
                         'Resetting to last known ID.')
            peer._id = savedPeerId
        peer._lastServerId = savedPeerId

    @peer.on(PeerEventType.Close)
    def peer_close():
        # peerConnection = null
        logger.info('Peer connection closed')

    @peer.on(PeerEventType.Error)
    def peer_error(err):
        logger.exception('Peer error {}', err)
        logger.warning('peerConnectionStatus {}', peerConnectionStatus)
        # retry peer connection in a few seconds
        # loop = asyncio.get_event_loop()
        # loop.call_later(3, pnp_service_connect)

    # remote peer tries to initiate connection
    @peer.on(PeerEventType.Connection)
    async def peer_connection(peerConnection):
        logger.info('Remote peer trying to establish connection')
        # register events for connection
        _set_conn_event_handlers(peerConnection)


def _set_conn_event_handlers(peerConnection):

    @peerConnection.on(ConnectionEventType.Open)
    async def pc_open():
        logger.info('Connected to: {}', peerConnection.peer)

    @peerConnection.on(ConnectionEventType.Stream)
    async def pc_stream(remoteStream):
        logger.info('pc_stream:',remoteStream)

    # Handle incoming data (messages only since this is the signal sender)
    @peerConnection.on(ConnectionEventType.Data)
    async def pc_data(data):
        logger.info('data received from remote peer \n{}', data)
        msg = json.loads(data)
        logger.info('data parse from remote peer \n{}', msg)

    @peerConnection.on(ConnectionEventType.Close)
    async def pc_close():
        logger.info('Connection to remote peer closed')


async def pnp_service_connect() -> Peer:
    """Create a Peer instance and register with PnP signaling server."""
    # Create own peer object with connection to shared PeerJS server
    logger.info('creating peer')
    # If we already have an assigned peerId, we will reuse it forever.
    # We expect that peerId is crypto secure. No need to replace.
    # Unless the user explicitly requests a refresh.
    global savedPeerId
    global config
    logger.info('last saved savedPeerId {}', savedPeerId)
    new_token = util.randomToken()
    logger.info('Peer session token {}', new_token)


    options = PeerOptions(
        host=config['signaling_server'],
        port=config['port'],
        secure=config['secure'],
        token=new_token,
        config=RTCConfiguration(
            iceServers=[RTCIceServer(**srv) for srv in config['ice_servers']]
        )
    )
    peerInst = Peer(id=savedPeerId, peer_options=options)
    logger.info('pnpService: peer created with id {} , options: {}',
        peerInst.id,
        peerInst.options)
    await peerInst.start()
    logger.info('peer activated')
    # Register events for this peer instance
    _set_peer_events(peerInst)
    return peerInst


async def make_discoverable(peer=None):
    """Enable remote peers to find and connect to this peer."""
    logger.debug('Enter peer discoverable.')
    logger.debug('Before _is_shutting_down')
    global _is_shutting_down
    global savedPeerId
    logger.debug('Making peer discoverable.')
    while not _is_shutting_down:
        logger.debug('Discovery loop.')
        logger.debug('peer status: {}', peer)
        try:
            if not peer or peer.destroyed:
                logger.info('Peer destroyed. Will create a new peer.')
                peer = await pnp_service_connect()
            elif peer.open:
                logger.info('Peer open: ', savedPeerId)
            elif peer.disconnected:
                logger.info('Peer disconnected. Will try to reconnect.')
                await peer.reconnect()
            else:
                logger.info('Peer still establishing connection. {}', peer)
        except Exception as e:
            logger.exception('Error while trying to join local peer room. '
                          'Will retry in a few moments. '
                          'Error: \n{}', e)
            if peer and not peer.destroyed:
                # something is not right with the connection to the server
                # lets start a fresh peer connection
                logger.info('Peer connection was corrupted. Detroying peer.')
                await peer.destroy()
                peer = None
                logger.debug('peer status after destroy: {}', peer)
        await asyncio.sleep(3)


def _config_logger():
    global config
    if config:
        log_level = config.get("log_level", "DEBUG")
    else:
        log_level = DEFAULT_LOG_LEVEL
    logger.remove()
    logger.add(sys.stdout, colorize=True, level=log_level, enqueue=True)
    logger.info(f'Log level is: {log_level}')

async def _start():
    global http_session
    http_session = aiohttp.ClientSession()
    global peer
    logger.info('Calling make_discoverable')
    await make_discoverable(peer=peer)
    logger.info('Exited make_discoverable')
    await logger.complete()

async def _shutdown():
    global _is_shutting_down
    _is_shutting_down = True
    global peer
    logger.debug('Shutting down. Peer {}', peer)
    if peer:
        logger.info('Destroying peer {}', peer)
        await peer.destroy()
    else:
        logger.info('Peer is None')
    # loop.run_until_complete(pc.close())
    # loop.run_until_complete(signaling.close())
    global http_session
    await http_session.close()

@logger.catch
def main():
    # args = None
    # parser = argparse.ArgumentParser(description="Data channels ping/pong")
    # parser.add_argument("role", choices=["offer", "answer"])
    # parser.add_argument("--verbose", "-v", action="count")
    # add_signaling_arguments(parser)
    # args = parser.parse_args()
    # if args.verbose:
    logger.info('Calling _loadPeerId()')
    _loadPeerId()
    logger.info('After _loadPeerId()')
    exists = _loadConfig()
    logger.info('Calling _loadConfig()')
    if not exists:
        _saveConfig()
    _config_logger()
    logger.info('After _loadConfig()')
    # add formatter to ch
    # signaling = create_signaling(args)
    # signaling = AmbianicPnpSignaling(args)
    # pc = RTCPeerConnection()
    # if args.role == "offer":
    #     coro = _run_offer(pc, signaling)
    # else:
    #     coro = _run_answer(pc, signaling)

    # run event loop
    loop = asyncio.get_event_loop()
    try:
        logger.info('\n>>>>> Starting http-proxy over webrtc. <<<<')
        loop.run_until_complete(_start())
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info('KeyboardInterrupt detected.')
    finally:
        logger.info('Shutting down...')
        loop.run_until_complete(_shutdown())
        loop.close()
        logger.info('All done.')


if __name__ == "__main__":
    main()
