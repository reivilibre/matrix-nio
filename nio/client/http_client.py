# -*- coding: utf-8 -*-

# Copyright © 2018, 2019 Damir Jelić <poljar@termina.org.uk>
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import json
import pprint
from builtins import str, super
from collections import deque
from functools import wraps
from typing import Any, Deque, Dict, List, Optional, Tuple, Type, Union
from uuid import UUID, uuid4

import attr
import h2
import h11
from logbook import Logger

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse  # type: ignore


from . import Client, ClientConfig
from .base_client import logged_in, store_loaded
from ..api import Api, MessageDirection, ResizingMethod
from ..events import MegolmEvent
from ..exceptions import LocalProtocolError, RemoteTransportError
from ..http import (Http2Connection, Http2Request, HttpConnection, HttpRequest,
                    TransportRequest, TransportResponse, TransportType)
from ..log import logger_group
from ..responses import (DeleteDevicesAuthResponse, DeleteDevicesResponse,
                         DevicesResponse, FileResponse,
                         JoinedMembersResponse, JoinResponse,
                         KeysClaimResponse, KeysQueryResponse, KeysUploadError,
                         KeysUploadResponse, LoginResponse, LogoutResponse,
                         PartialSyncResponse, ProfileGetAvatarResponse,
                         ProfileGetDisplayNameResponse, ProfileGetResponse,
                         ProfileSetAvatarResponse,
                         ProfileSetDisplayNameResponse, Response,
                         RoomForgetResponse, RoomInviteResponse,
                         RoomKeyRequestResponse, RoomKickResponse,
                         RoomLeaveResponse, RoomMessagesResponse,
                         RoomPutStateResponse, RoomReadMarkersResponse,
                         RoomRedactResponse, RoomSendResponse,
                         RoomTypingResponse, ShareGroupSessionResponse,
                         SyncResponse, ThumbnailResponse,
                         ToDeviceResponse, UpdateDeviceResponse,
                         LoginInfoResponse)

if False:
    from .messages import ToDeviceMessage
    from .crypto import OlmDevice

try:
    from json.decoder import JSONDecodeError
except ImportError:
    JSONDecodeError = ValueError  # type: ignore


logger = Logger("nio.client")
logger_group.add_logger(logger)


def connected(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.connection:
            raise LocalProtocolError("Not connected.")
        return func(self, *args, **kwargs)
    return wrapper


@attr.s
class RequestInfo(object):
    request_class = attr.ib(type=Type[Response])
    extra_data = attr.ib(default=None, type=Tuple)


class HttpClient(Client):
    def __init__(
        self,
        homeserver,  # type: str
        user="",  # type: str
        device_id="",  # type: Optional[str]
        store_path="",  # type: Optional[str]
        config=None,  # type: Optional[ClientConfig]
    ):
        # type: (...) -> None
        self.host, self.extra_path = HttpClient._parse_homeserver(homeserver)
        self.requests_made = {}  # type: Dict[UUID, RequestInfo]
        self.parse_queue = deque()  \
            # type: Deque[Tuple[RequestInfo, TransportResponse]]
        self.partial_sync = None  # type: Optional[PartialSyncResponse]

        self.connection = None \
            # type: Optional[Union[HttpConnection, Http2Connection]]

        super().__init__(user, device_id, store_path, config)

    @staticmethod
    def _parse_homeserver(homeserver):
        if not homeserver.startswith("http"):
            homeserver = "https://{}".format(homeserver)

        homeserver = urlparse(homeserver)

        if homeserver.port:
            port = homeserver.port
        else:
            if homeserver.scheme == "https":
                port = 443
            elif homeserver.scheme == "http":
                port = 80
            else:
                raise ValueError("Invalid URI scheme for Homeserver")

        host = "{}:{}".format(homeserver.hostname, port)
        extra_path = homeserver.path.strip("/")

        return host, extra_path

    @connected
    def _send(
        self,
        request,       # type: TransportRequest
        request_info,  # type: RequestInfo
        uuid=None      # type: Optional[UUID]
    ):
        # type: (...) -> Tuple[UUID, bytes]
        assert self.connection

        ret_uuid, data = self.connection.send(request, uuid)
        self.requests_made[ret_uuid] = request_info
        return ret_uuid, data

    def _add_extra_path(self, path):
        if self.extra_path:
            return "/{}{}".format(self.extra_path, path)
        return path

    def _build_request(self, api_response, timeout=0):
        def unpack_api_call(method, *rest):
            return method, rest

        method, api_data = unpack_api_call(*api_response)

        if isinstance(self.connection, HttpConnection):
            if method == "GET":
                path = self._add_extra_path(api_data[0])
                return HttpRequest.get(self.host, path, timeout)
            elif method == "POST":
                path, data = api_data
                path = self._add_extra_path(path)
                return HttpRequest.post(self.host, path, data, timeout)
            elif method == "PUT":
                path, data = api_data
                path = self._add_extra_path(path)
                return HttpRequest.put(self.host, path, data, timeout)
        elif isinstance(self.connection, Http2Connection):
            if method == "GET":
                path = api_data[0]
                path = self._add_extra_path(path)
                return Http2Request.get(self.host, path, timeout)
            elif method == "POST":
                path, data = api_data
                path = self._add_extra_path(path)
                return Http2Request.post(self.host, path, data, timeout)
            elif method == "PUT":
                path, data = api_data
                path = self._add_extra_path(path)
                return Http2Request.put(self.host, path, data, timeout)

        assert("Invalid connection type")

    @property
    def lag(self):
        # type: () -> float
        if not self.connection:
            return 0

        return self.connection.elapsed

    def connect(self, transport_type=TransportType.HTTP):
        # type: (Optional[TransportType]) -> bytes
        if transport_type == TransportType.HTTP:
            self.connection = HttpConnection()
        elif transport_type == TransportType.HTTP2:
            self.connection = Http2Connection()
        else:
            raise NotImplementedError

        return self.connection.connect()

    def _clear_queues(self):
        self.requests_made.clear()
        self.parse_queue.clear()

    @connected
    def disconnect(self):
        # type: () -> bytes
        assert self.connection

        data = self.connection.disconnect()
        self._clear_queues()
        self.connection = None
        return data

    @connected
    def data_to_send(self):
        # type: () -> bytes
        assert self.connection
        return self.connection.data_to_send()

    @connected
    def login_info(self):
        # type: () -> Tuple[UUID, bytes]
        """Get the available login methods from the server

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        """
        request = self._build_request(Api.login_info())

        return self._send(request, RequestInfo(LoginInfoResponse))

    @connected
    def login(self, password=None, device_name="", token=None):
        # type: (str, Optional[str]) -> Tuple[UUID, bytes]
        if password is None and token is None:
            raise ValueError("Either a password or a token needs to be "
                             "provided")

        request = self._build_request(
            Api.login(
                self.user,
                password=password,
                device_name=device_name,
                device_id=self.device_id,
                token=token
            )
        )

        return self._send(request, RequestInfo(LoginResponse))

    @connected
    @logged_in
    def logout(self):
        request = self._build_request(
            Api.logout(
                self.access_token
            )
        )

        return self.send(request, RequestInfo(LogoutResponse))

    @connected
    @logged_in
    def room_send(self, room_id, message_type, content, tx_id=None):
        if self.olm:
            try:
                room = self.rooms[room_id]
            except KeyError:
                raise LocalProtocolError(
                    "No such room with id {} found.".format(room_id)
                )

            if room.encrypted:
                content = self.olm.group_encrypt(
                    room_id,
                    {
                        "content": content,
                        "type": message_type
                    },
                )
                message_type = "m.room.encrypted"

        uuid = tx_id or uuid4()

        request = self._build_request(
            Api.room_send(
                self.access_token,
                room_id,
                message_type,
                content,
                uuid
            )
        )

        return self._send(
            request,
            RequestInfo(RoomSendResponse, (room_id, )),
            uuid
        )

    @connected
    @logged_in
    def room_put_state(self, room_id, event_type, body):
        request = self._build_request(
            Api.room_put_state(
                self.access_token,
                room_id,
                event_type,
                body
            )
        )

        return self._send(
            request,
            RequestInfo(RoomPutStateResponse, (room_id, ))
        )

    @connected
    @logged_in
    def room_redact(self, room_id, event_id, reason=None, tx_id=None):
        uuid = tx_id or uuid4()

        request = self._build_request(
            Api.room_redact(
                self.access_token,
                room_id,
                event_id,
                tx_id,
                reason=reason,
            )
        )

        return self._send(
            request,
            RequestInfo(RoomRedactResponse, (room_id, )),
            uuid
        )

    @connected
    @logged_in
    def room_kick(self, room_id, user_id, reason=None):
        request = self._build_request(
            Api.room_kick(
                self.access_token,
                room_id,
                user_id,
                reason=reason
            )
        )

        return self._send(
            request,
            RequestInfo(RoomKickResponse)
        )

    @connected
    @logged_in
    def room_invite(self, room_id, user_id):
        request = self._build_request(
            Api.room_invite(
                self.access_token,
                room_id,
                user_id
            )
        )

        return self._send(request, RequestInfo(RoomInviteResponse))

    @connected
    @logged_in
    def join(self, room_id):
        # type: (str) -> Tuple[UUID, bytes]
        """Join a room.

        This tells the server to join the given room.
        If the room is not public, the user must be invited.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            room_id: The room id or alias of the room to join.
        """
        request = self._build_request(Api.join(self.access_token, room_id))
        return self._send(request, RequestInfo(JoinResponse))

    @connected
    @logged_in
    def room_leave(self, room_id):
        # type: (str) -> Tuple[UUID, bytes]
        """Leave a room or reject an invite.

        This tells the server to leave the given room.
        If the user was only invited, the invite is rejected.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            room_id: The room id of the room to leave.
        """
        request = self._build_request(
            Api.room_leave(
                self.access_token,
                room_id
            )
        )
        return self._send(request, RequestInfo(RoomLeaveResponse))

    @connected
    @logged_in
    def room_forget(self, room_id):
        # type: (str) -> Tuple[UUID, bytes]
        """Forget a room.

        This tells the server to forget the given room's history for our user.
        If all users on a homeserver forget the room, the room will be
        eligible for deletion from that homeserver.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            room_id (str): The room id of the room to forget.
        """
        request = self._build_request(
            Api.room_forget(
                self.access_token,
                room_id
            )
        )
        return self._send(request, RequestInfo(
            RoomForgetResponse,
            (room_id,)
        ))

    @connected
    @logged_in
    def room_messages(
        self,
        room_id,
        start,
        end=None,
        direction=MessageDirection.back,
        limit=10
    ):
        request = self._build_request(
            Api.room_messages(
                self.access_token,
                room_id,
                start,
                end=end,
                direction=direction,
                limit=limit
            )
        )
        return self._send(
            request,
            RequestInfo(RoomMessagesResponse, (room_id,))
        )

    @connected
    @logged_in
    def room_typing(
        self,
        room_id,            # type: str
        typing_state=True,  # type: bool
        timeout=30000       # type: int
    ):
        # type: (...) -> Tuple[UUID, bytes]
        """Send a typing notice to the server.

        This tells the server that the user is typing for the next N
        milliseconds or that the user has stopped typing.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            room_id (str): Room id of the room where the user is typing.
            typign_state (bool): A flag representing whether the user started
                or stopped typing
            timeout (int): For how long should the new typing notice be
                valid for in milliseconds.
        """
        request = self._build_request(
            Api.room_typing(
                self.access_token,
                room_id,
                self.user_id,
                typing_state,
                timeout
            )
        )
        return self._send(request, RequestInfo(
            RoomTypingResponse,
            (room_id, )
        ))

    @connected
    @logged_in
    def room_read_markers(
        self,
        room_id,            # type: str
        fully_read_event,   # type: str
        read_event=None,    # type: Optional[str]
    ):
        # type: (...) -> Tuple[UUID, bytes]
        """Update read markers for a room.

        This sets the position of the read marker for a given room,
        and optionally the read receipt's location.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            room_id (str): Room id of the room of the room where the read
                markers should be updated
            fully_read_event (str): The event ID the read marker should be
                located at.
            read_event (Optional[str]): The event ID to set the read
                receipt location at.
        """
        request = self._build_request(
            Api.room_read_markers(
                self.access_token,
                room_id,
                fully_read_event,
                read_event
            )
        )
        return self._send(request, RequestInfo(
            RoomReadMarkersResponse,
            (room_id, )
        ))

    @connected
    @logged_in
    def thumbnail(
        self,
        server_name,                  # type: str
        media_id,                     # type: str
        width,                        # type: int
        height,                       # type: int
        method=ResizingMethod.scale,  # ŧype: ResizingMethod
        allow_remote=True,            # type: bool
    ):
        # type: (...) -> Tuple[UUID, bytes]
        """Get the thumbnail of a file from the content repository.

        Note: The actual thumbnail may be larger than the size specified.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            server_name (str): The server name from the mxc:// URI.
            media_id (str): The media ID from the mxc:// URI.
            width (int): The desired width of the thumbnail.
            height (int): The desired height of the thumbnail.
            method (ResizingMethod): The desired resizing method.
            allow_remote (bool): Indicates to the server that it should not
                attempt to fetch the media if it is deemed remote.
                This is to prevent routing loops where the server contacts
                itself.
        """
        request = self._build_request(
            Api.thumbnail(
                self.access_token,
                server_name,
                media_id,
                width,
                height,
                method,
                allow_remote
            )
        )

        return self._send(request, RequestInfo(ThumbnailResponse))

    @connected
    @logged_in
    @store_loaded
    def keys_upload(self):
        keys_dict = self.olm.share_keys()

        logger.debug(pprint.pformat(keys_dict))

        request = self._build_request(
            Api.keys_upload(
                self.access_token,
                keys_dict
            )
        )
        return self._send(request, RequestInfo(KeysUploadResponse))

    @connected
    @logged_in
    @store_loaded
    def keys_query(self):
        """Query the server for user keys.

        This queries the server for device keys of users with which we share an
        encrypted room.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.
        """
        user_list = [
            user_id for room in self.rooms.values()
            if room.encrypted for user_id in room.users
        ]

        if not user_list:
            raise LocalProtocolError("No key query required.")

        request = self._build_request(
            Api.keys_query(
                self.access_token,
                user_list
            )
        )
        return self._send(request, RequestInfo(KeysQueryResponse))

    @connected
    @logged_in
    @store_loaded
    def keys_claim(self, room_id):
        user_list = self.get_missing_sessions(room_id)

        request = self._build_request(
            Api.keys_claim(
                self.access_token,
                user_list
            )
        )
        return self._send(
            request,
            RequestInfo(KeysClaimResponse, (room_id, ))
        )

    @connected
    @logged_in
    @store_loaded
    def share_group_session(
        self,
        room_id,
        ignore_missing_sessions=False,
        tx_id=None,
        ignore_unverified_devices=False
    ):
        # type: (str, bool, str, bool) -> Tuple[UUID, bytes]
        """Share a group session with a room.

        This method sends a group session to members of a room.

        Args:
            room_id(str): The room id of the room where the message should be
                sent to.
            tx_id(str, optional): The transaction ID of this event used to
                uniquely identify this message.
            ignore_unverified_devices(bool): Mark unverified devices as
                ignored. Ignored devices will still receive encryption
                keys for messages but they won't be marked as verified.

        Raises LocalProtocolError if the client isn't logged in, if the session
        store isn't loaded, no room with the given room id exists or the room
        isn't an encrypted room.
        """

        assert self.olm
        try:
            room = self.rooms[room_id]
        except KeyError:
            raise LocalProtocolError("No such room with id {}".format(room_id))

        if not room.encrypted:
            raise LocalProtocolError("Room with id {} is not encrypted".format(
                room_id))

        user_map, to_device_dict = self.olm.share_group_session(
            room_id,
            list(room.users.keys()),
            ignore_missing_sessions,
            ignore_unverified_devices
        )

        uuid = tx_id or uuid4()

        request = self._build_request(
            Api.to_device(
                self.access_token,
                "m.room.encrypted",
                to_device_dict,
                uuid
            )
        )

        return self._send(
            request,
            RequestInfo(ShareGroupSessionResponse, (room_id, user_map))
        )

    @connected
    @logged_in
    def devices(self):
        # type: () -> Tuple[UUID, bytes]
        request = self._build_request(Api.devices(self.access_token))
        return self._send(request, RequestInfo(DevicesResponse))

    @connected
    @logged_in
    def update_device(self, device_id, content):
        # type: (str, Dict[str, str]) -> Tuple[UUID, bytes]
        request = self._build_request(
            Api.update_device(
                self.access_token,
                device_id,
                content
            )
        )

        return self._send(request, RequestInfo(UpdateDeviceResponse))

    @connected
    @logged_in
    def delete_devices(self, devices, auth=None):
        # type: (List[str], Optional[Dict[str, str]]) -> Tuple[UUID, bytes]
        request = self._build_request(
            Api.delete_devices(
                self.access_token,
                devices,
                auth_dict=auth
            )
        )

        return self._send(request, RequestInfo(DeleteDevicesResponse))

    @connected
    @logged_in
    def joined_members(self, room_id):
        # type: (str) -> Tuple[UUID, bytes]
        request = self._build_request(
            Api.joined_members(
                self.access_token,
                room_id
            )
        )

        return self._send(
            request,
            RequestInfo(JoinedMembersResponse, (room_id, ))
        )

    @connected
    @logged_in
    def get_profile(self, user_id=None):
        # type: (Optional[str]) -> Tuple[UUID, bytes]
        """Get a user's combined profile information.

        This queries the display name and avatar matrix content URI of a user
        from the server. Additional profile information may be present.
        The currently logged in user is queried if no user is specified.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            user_id (str): User id of the user to get the profile for.
        """
        request = self._build_request(Api.profile_get(
            self.access_token,
            user_id or self.user_id
        ))
        return self._send(
            request,
            RequestInfo(ProfileGetResponse)
        )

    @connected
    @logged_in
    def get_displayname(self, user_id=None):
        # type: (Optional[str]) -> Tuple[UUID, bytes]
        """Get a user's display name.

        This queries the display name of a user from the server.
        The currently logged in user is queried if no user is specified.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            user_id (str): User id of the user to get the display name for.
        """
        request = self._build_request(Api.profile_get_displayname(
            self.access_token,
            user_id or self.user_id
        ))
        return self._send(
            request,
            RequestInfo(ProfileGetDisplayNameResponse)
        )

    @connected
    @logged_in
    def set_displayname(self, displayname):
        # type: (str) -> Tuple[UUID, bytes]
        """Set the user's display name.

        This tells the server to set the display name of the currently logged
        in user to supplied string.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            displayname (str): Display name to set.
        """
        request = self._build_request(Api.profile_set_displayname(
            self.access_token,
            self.user_id,
            displayname
        ))
        return self._send(
            request,
            RequestInfo(ProfileSetDisplayNameResponse)
        )

    @connected
    @logged_in
    def get_avatar(self, user_id=None):
        # type: (Optional[str]) -> Tuple[UUID, bytes]
        """Get a user's avatar URL.

        This queries the avatar matrix content URI of a user from the server.
        The currently logged in user is queried if no user is specified.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            user_id (str): User id of the user to get the avatar for.
        """
        request = self._build_request(Api.profile_get_avatar(
            self.access_token,
            user_id or self.user_id
        ))
        return self._send(
            request,
            RequestInfo(ProfileGetAvatarResponse)
        )

    @connected
    @logged_in
    def set_avatar(self, avatar_url):
        # type: (str) -> Tuple[UUID, bytes]
        """Set the user's avatar URL.

        This tells the server to set avatar of the currently logged
        in user to supplied matrix content URI.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            avatar_url (str): matrix content URI of the avatar to set.
        """
        request = self._build_request(Api.profile_set_avatar(
            self.access_token,
            self.user_id,
            avatar_url
        ))
        return self._send(
            request,
            RequestInfo(ProfileSetAvatarResponse)
        )

    @connected
    @logged_in
    @store_loaded
    def request_room_key(self, event, tx_id=None):
        # type: (MegolmEvent, Optional[str]) -> Tuple[UUID, bytes]
        """Request a missing room key.

        This sends out a message to other devices requesting a room key from
        them.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            event (str): An undecrypted MegolmEvent for which we would like to
                request the decryption key.
        """
        uuid = tx_id or uuid4()

        if event.session_id in self.outgoing_key_requests:
            raise LocalProtocolError("A key sharing request is already sent"
                                     " out for this session id.")

        assert self.user_id
        assert self.device_id

        message = event.as_key_request(self.user_id, self.device_id)

        request = self._build_request(Api.to_device(
            self.access_token,
            message.type,
            message.as_dict(),
            uuid
        ))
        return self._send(
            request,
            RequestInfo(
                RoomKeyRequestResponse,
                (
                    event.session_id,
                    event.session_id,
                    event.room_id,
                    event.algorithm
                )
            )
        )

    @connected
    @logged_in
    @store_loaded
    def confirm_short_auth_string(self, transaction_id, tx_id=None):
        # type: (str, Optional[str]) -> Tuple[UUID, bytes]
        """Confirm a short auth string and mark it as matching.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            transaction_id (str): An transaction id of a valid key verification
                process.
        """
        message = self.confirm_key_verification(transaction_id)
        return self.to_device(message)

    @connected
    @logged_in
    @store_loaded
    def start_key_verification(self, device, tx_id=None):
        # type: (OlmDevice, Optional[str]) -> Tuple[UUID, bytes]
        """Start a interactive key verification with the given device.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            device (OlmDevice): An device with which we would like to start the
                interactive key verification process.
        """
        message = self.create_key_verification(device)
        return self.to_device(message, tx_id)

    @connected
    @logged_in
    @store_loaded
    def accept_key_verification(self, transaction_id, tx_id=None):
        # type: (str, Optional[str]) -> Tuple[UUID, bytes]
        """Accept a key verification start event.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            transaction_id (str): An transaction id of a valid key verification
                process.
        """
        if transaction_id not in self.key_verifications:
            raise LocalProtocolError("Key verification with the transaction "
                                     "id {} does not exist.".format(
                                         transaction_id
                                     ))

        sas = self.key_verifications[transaction_id]

        message = sas.accept_verification()

        return self.to_device(message, tx_id)

    @connected
    @logged_in
    @store_loaded
    def cancel_key_verification(self, transaction_id, tx_id=None):
        # type: (str, Optional[str]) -> Tuple[UUID, bytes]
        """Abort an interactive key verification.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            transaction_id (str): An transaction id of a valid key verification
                process.
        """
        if transaction_id not in self.key_verifications:
            raise LocalProtocolError("Key verification with the transaction "
                                     "id {} does not exist.".format(
                                         transaction_id
                                     ))

        sas = self.key_verifications[transaction_id]
        sas.cancel()

        message = sas.get_cancellation()

        return self.to_device(message, tx_id)

    @logged_in
    @store_loaded
    def to_device(
        self,
        message,
        tx_id=None
    ):
        # type: (ToDeviceMessage, Optional[str]) -> Tuple[UUID, bytes]
        """Send a message to a specific device.

        Returns a unique uuid that identifies the request and the bytes that
        should be sent to the socket.

        Args:
            message (ToDeviceMessage): The message that should be sent out.
            tx_id (str, optional): The transaction ID for this message. Should
                be unique.
        """
        uuid = tx_id or uuid4()

        request = self._build_request(Api.to_device(
            self.access_token,
            message.type,
            message.as_dict(),
            uuid
        ))
        return self._send(request, RequestInfo(ToDeviceResponse, (message, )))

    @connected
    @logged_in
    def sync(self, timeout=None, filter=None, full_state=False):
        # type: (Optional[int], Optional[Dict[Any, Any]]) -> Tuple[UUID, bytes]
        request = self._build_request(
            Api.sync(
                self.access_token,
                since=self.next_batch or self.loaded_sync_token,
                timeout=timeout,
                filter=filter,
                full_state=full_state
            ),
            timeout
        )

        return self._send(request, RequestInfo(SyncResponse))

    def parse_body(self, transport_response):
        # type: (TransportResponse) -> Dict[Any, Any]
        """Parse the body of the response.

        Args:
            transport_response(TransportResponse): The transport response that
                contains the body of the response.

        Returns a dictionary representing the response.
        """
        try:
            return json.loads(transport_response.text, encoding="utf-8")
        except JSONDecodeError:
            return {}

    def _create_response(self, request_info, transport_response, max_events=0):
        request_class = request_info.request_class
        extra_data = request_info.extra_data or ()

        try:
            content_type = str(
                transport_response.headers[b"content-type"], "utf-8"
            )
        except KeyError:
            content_type = None

        is_json = content_type == "application/json"

        if issubclass(request_class, FileResponse) and is_json:
            parsed_dict = self.parse_body(transport_response)
            response = request_class.from_data(
                parsed_dict, content_type, *extra_data
            )

        elif issubclass(request_class, FileResponse):
            body = transport_response.content
            response = request_class.from_data(body, content_type, *extra_data)

        else:
            parsed_dict = self.parse_body(transport_response)

            if (transport_response.status_code == 401
                    and request_class == DeleteDevicesResponse):
                response = DeleteDevicesAuthResponse.from_dict(parsed_dict)

            response = request_class.from_dict(parsed_dict, *extra_data)

        assert response

        logger.info("Received new response of type {}".format(
            response.__class__.__name__
        ))

        response.start_time = transport_response.send_time
        response.end_time = transport_response.receive_time
        response.timeout = transport_response.timeout
        response.status_code = transport_response.status_code
        response.uuid = transport_response.uuid

        return response

    def handle_key_upload_error(self, response):
        if not self.olm:
            return

        if response.status_code in [400, 500]:
            self.olm.mark_keys_as_published()
            self.olm.save_account()

    @connected
    def receive(self, data):
        # type: (bytes) -> None
        """Pass received data to the client"""
        assert self.connection

        try:
            response = self.connection.receive(data)
        except (h11.RemoteProtocolError, h2.exceptions.ProtocolError) as e:
            raise RemoteTransportError(e)

        if response:
            try:
                request_info = self.requests_made.pop(response.uuid)
            except KeyError:
                logger.error("{}".format(pprint.pformat(self.requests_made)))
                raise

            if response.is_ok:
                logger.info(
                    "Received response of type: {}".format(
                        request_info.request_class
                    )
                )
            else:
                logger.info(
                    (
                        "Error with response of type type: {}, "
                        "error code {}"
                    ).format(request_info.request_class, response.status_code)
                )

            self.parse_queue.append((request_info, response))
        return

    def next_response(self, max_events=0):
        # type: (int) -> Optional[Union[TransportResponse, Response]]
        if not self.parse_queue and not self.partial_sync:
            return None

        if self.partial_sync:
            sync_response = self.partial_sync.next_part(max_events)
            self.receive_response(sync_response)

            if isinstance(sync_response, PartialSyncResponse):
                self.partial_sync = sync_response
            else:
                self.partial_sync = None

            return sync_response

        request_info, transport_response = self.parse_queue.popleft()
        response = self._create_response(
            request_info,
            transport_response,
            max_events
        )

        if isinstance(response, PartialSyncResponse):
            self.partial_sync = response

        elif isinstance(response, KeysUploadError):
            self.handle_key_upload_error(response)

        self.receive_response(response)

        return response
