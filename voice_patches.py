# voice_patches.py
import logging

import davey
from nacl.exceptions import CryptoError
from discord.ext.voice_recv import rtp
from discord.ext.voice_recv.reader import AudioReader
from discord.ext.voice_recv.router import PacketRouter

log = logging.getLogger(__name__)


# discord-ext-voice-recv (0.5.2a179) has no DAVE (Discord E2EE) support: it RTP-decrypts
# incoming packets but hands the still-DAVE-encrypted Opus payload straight to the Opus
# decoder, which fails with OpusError: corrupted stream. This patches AudioReader.callback
# to DAVE-decrypt each packet (via the davey session discord.py already negotiates) right
# after RTP decryption, before it reaches the decoder.
def _dave_aware_callback(self, packet_data: bytes) -> None:
    packet = rtp_packet = rtcp_packet = None
    try:
        if not rtp.is_rtcp(packet_data):
            packet = rtp_packet = rtp.decode_rtp(packet_data)
            packet.decrypted_data = self.decryptor.decrypt_rtp(packet)

            conn = self.voice_client._connection
            if conn.dave_session and conn.dave_session.ready:
                user_id = self.voice_client._ssrc_to_id.get(rtp_packet.ssrc)
                if user_id is not None and not conn.dave_session.can_passthrough(user_id):
                    try:
                        packet.decrypted_data = conn.dave_session.decrypt(
                            user_id, davey.MediaType.audio, packet.decrypted_data
                        )
                    except Exception as e:
                        log.debug("[DAVE] Decrypt failed for ssrc=%s user=%s: %s", rtp_packet.ssrc, user_id, e)
        else:
            packet = rtcp_packet = rtp.decode_rtcp(self.decryptor.decrypt_rtcp(packet_data))

            if not isinstance(packet, rtp.ReceiverReportPacket):
                log.info("Received unexpected rtcp packet: type=%s, %s", packet.type, type(packet))
    except CryptoError:
        log.error("CryptoError decoding packet data")
        log.debug("CryptoError details:\n  data=%s\n  secret_key=%s", packet_data, self.voice_client.secret_key)
        return
    except Exception:
        if self._is_ip_discovery_packet(packet_data):
            log.debug("Ignoring ip discovery packet")
            return

        log.exception("Error unpacking packet")
        log.debug("Packet data: len=%s data=%s", len(packet_data), packet_data)
    finally:
        if self.error:
            self.stop()
            return
        if not packet:
            return

    if rtcp_packet:
        self.packet_router.feed_rtcp(rtcp_packet)
    elif rtp_packet:
        ssrc = rtp_packet.ssrc

        if ssrc not in self.voice_client._ssrc_to_id:
            if rtp_packet.is_silence():
                log.debug("Skipping silence packet for unknown ssrc %s", ssrc)
                return
            else:
                log.info("Received packet for unknown ssrc %s:\n%s", ssrc, rtp_packet)

        self.speaking_timer.notify(ssrc)
        try:
            self.packet_router.feed_rtp(rtp_packet)
        except Exception as e:
            log.exception("Error processing rtp packet")
            self.error = e
            self.stop()


# The real killer: PacketRouter.run() catches ANY exception from a single packet's
# decode (e.g. one corrupted/dropped UDP packet — normal on real networks, not just a
# DAVE problem) and responds by calling voice_client.stop_listening() in its `finally`.
# One bad packet anywhere in the call permanently kills the whole listening session —
# every later packet is just dropped with no further logging. This patches the decode
# loop to drop only the one bad packet and keep going.
def _resilient_do_run(self) -> None:
    while not self._end_thread.is_set():
        self.waiter.wait()
        with self._lock:
            for decoder in self.waiter.items:
                try:
                    data = decoder.pop_data()
                except Exception:
                    log.exception("[voice_patches] Dropping bad packet for ssrc=%s", decoder.ssrc)
                    continue
                if data is not None:
                    self.sink.write(data.source, data)


def apply() -> None:
    AudioReader.callback = _dave_aware_callback
    PacketRouter._do_run = _resilient_do_run
    print("[voice_patches] Patched discord-ext-voice-recv: DAVE decryption + per-packet decode resilience")
