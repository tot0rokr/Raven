FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=Asia/Seoul

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gpg fontconfig fonts-dejavu-core tzdata procps xauth \
      openbox tigervnc-standalone-server tigervnc-tools tigervnc-viewer \
      xtightvncviewer xinput x11-xserver-utils x11-apps xterm tint2 dbus-x11 \
 && mkdir -p /usr/local/share/fonts/NotoSansKR \
 && curl -fsSL -o '/usr/local/share/fonts/NotoSansKR/NotoSansKR[wght].ttf' \
      'https://github.com/google/fonts/raw/main/ofl/notosanskr/NotoSansKR%5Bwght%5D.ttf' \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

ARG UID=1000
ARG GID=1000
# -l: skip lastlog; a large UID would otherwise create a huge sparse file
RUN userdel -r ubuntu 2>/dev/null || true \
 && groupadd -g "$GID" vdesk \
 && useradd -l -m -u "$UID" -g "$GID" -s /bin/bash vdesk \
 && mkdir -p /home/vdesk/Downloads \
 && chown -R vdesk:vdesk /home/vdesk

COPY rootfs/ /

USER vdesk
WORKDIR /home/vdesk
ENTRYPOINT ["/usr/local/bin/vdesk-entrypoint"]
