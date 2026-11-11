# Video over Starlink only

The car's camera stream (go2rtc, about 0.9 Mbit/s per viewer) is bigger than
the telemetry and must not share the HaLow link with it. Telemetry reaches the
car through the host's Tailscale node, which uses the HaLow LAN whenever it
can. Video gets a second Tailscale node on the car that is firewalled off the
HaLow port, so it can only travel over Starlink. No Starlink, no video.

## Parts

- `tailscaled-video.service` — a second tailscaled on the vehicle: userspace
  networking, unprivileged user `tsvideo`, UDP 41642, state in
  `/var/lib/tailscale-video`. In userspace mode inbound TCP to the node's
  tailnet address is handed to `127.0.0.1:<port>`, so `http://<node>:1984` is
  the host's go2rtc.
- `video-starlink-only.nft` — drops everything `tsvideo` sends out of `end0`
  (the HaLow port), anything arriving on `end0` for UDP 41642, and go2rtc's
  own ports (1984, 8554, 8555) on `end0` and `tailscale0`. The last rule
  matters: a WebRTC player negotiates media directly with the car, and before
  it was added the media went straight over HaLow even though the page had
  come through the relay.
- `video-relay` in `deploy/pit-compose.yaml` (profile `video`) — forwards
  `<pit-host>:1984` to the video node so phones on the pit LAN can watch
  without being on the tailnet. Set `OPENLAPS_VIDEO_UPSTREAM` to the video
  node's tailnet address and `COMPOSE_PROFILES=video` in the pit `.env`.

## Install (vehicle)

    sudo useradd -r -s /usr/sbin/nologin tsvideo
    sudo install -m644 tailscaled-video.service /etc/systemd/system/
    sudo install -m644 video-starlink-only.nft /etc/nftables.d/
    echo 'include "/etc/nftables.d/video-starlink-only.nft"' | sudo tee -a /etc/nftables.conf
    sudo nft -f /etc/nftables.d/video-starlink-only.nft
    sudo systemctl enable --now tailscaled-video
    sudo tailscale --socket=/run/tailscale-video/tailscaled.sock up \
        --hostname=openlaps-car-video --accept-dns=false

Disable key expiry for the new node in the Tailscale admin console.

## Watching

`http://<pit-host>:1984` or the `video` dashboard. Use `mode=mse` with
`car_h264`: MSE is TCP and resends what Starlink drops. `mode=webrtc` works
only where the browser can reach the car's Starlink address directly, has no
resend, and should be paired with `car_rtc` (a keyframe every half second).

## Measured 2026-11-11 (Luckfox, Insta360 X3, both ends on Starlink)

- 30 fps, no gaps, about 780 kbit/s through the relay.
- With the vehicle's Starlink port down, video failed and telemetry carried on
  over HaLow; video returned about 10 s after the port came back.
- A viewer at the camp reaches the pit relay across the HaLow mesh, so camp
  viewing does load the camp-to-pit hop.
