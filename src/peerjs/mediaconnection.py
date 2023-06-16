"""Wraps the streaming interface between two Peers."""
import logging
from typing import Any

from aiortc import RTCDataChannel,MediaStreamTrack

from .baseconnection import BaseConnection
from .enums import (
    ConnectionEventType,
    ConnectionType,
    ServerMessageType,
)
from .negotiator import Negotiator
from .servermessage import ServerMessage
from .util import util

log = logging.getLogger(__name__)

class MediaConnection(BaseConnection):
    """Wraps WebRTC's media streams."""

    ID_PREFIX = "mc_"

    @property
    def type(self):
        """Return ConnectionType.Media."""
        return ConnectionType.Media

    def localStream(self) -> MediaStreamTrack:
        """Return current data buffer size."""
        return self._localStream

    def remoteStream(self) -> MediaStreamTrack:
        """Return current data buffer size."""
        return self._remoteStream

    def __init__(self,
                 peerId: str = None,
                 provider=None,  # provider: Peer
                 **options
                 ):
        """Create a DataConnection instance."""
        super().__init__(peerId, provider, **options)
        self.options = options

        def _apply_options(
            connectionId: str = None,
            _stream: str = None,
            _payload: Any = None,
            **kwargs
              ):
            self.connectionId: str =  connectionId or MediaConnection.ID_PREFIX + util.randomToken()
            self._localStream: MediaStreamTrack = _stream or None
            self._payload = _payload
            self._payload['_stream']= _stream  or None

        log.debug('Applying MediaConnection options:\n%r', options)
        _apply_options(**options)
        self.peerId = peerId
        self._negotiator: Negotiator = None

        self._encodingQueue = None  # EncodingQueue()

        self._negotiator = Negotiator(self)

    async def start(self):
        """Start media connection negotiation."""
        payload_options = {'originator': False, 'constraints': None, '_stream' : self._localStream}
        log.debug('Start media payload_options: \n%r',
                  payload_options)
        await self._negotiator.startConnection(**payload_options)

    async def close(self) -> None:
        """Close this connection."""
        self._buffer = []
        self._bufferSize = 0
        self._chunkedData = {}
        if self._negotiator:
            await self._negotiator.cleanup()
            self._negotiator = None
        if self.provider:  # provider: Peer
            self.provider._removeConnection(self)
        self.provider = None
        if self.options and self.options._stream:
            self.options._stream = None

        if not self.open:
            return
        self._open = False
        self.emit(ConnectionEventType.Close)

    async def handleMessage(self, message: ServerMessage) -> None:
        """Handle signaling server message."""
        payload = message.payload
        if message.type == ServerMessageType.Answer:
            await self._negotiator.handleSDP(message.type, payload.sdp)
        elif message.type == ServerMessageType.Candidate:
            await self._negotiator.handleCandidate(payload['candidate'])
        else:
            log.warning(
              f"Unrecognized message type: {message.type}"
              "from peer: {this.peer}"
            )
    def addStream(self, remoteStream: MediaStreamTrack) -> None:
        """Handle signaling server message."""

        print('addStream')
        self._remoteStream = remoteStream
        self.emit(ConnectionEventType.Stream, remoteStream)

    async def answer(self, stream: MediaStreamTrack, options: Any = {}):
        if self._localStream:
            log.warning(
                "Local stream already exists on this MediaConnection. Are you answering a call twice?",
            )
            return
        self._localStream = stream
        if options and options.sdpTransform:
            self.options.sdpTransform = options.sdpTransform

        await self._negotiator.startConnection( originator=False, sdp=self._payload['sdp'], _stream=stream)

        messages = self.provider._getMessages(self.connectionId)
        for message in messages:
            self.handleMessage(message)

        self._open = True