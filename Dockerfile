# xmonad is configured in Haskell; compile the config here so the image needs no GHC
FROM ubuntu:24.04 AS xmonad-build

RUN apt-get update \
 && apt-get install -y --no-install-recommends ghc libghc-xmonad-contrib-dev \
 && rm -rf /var/lib/apt/lists/*

COPY xmonad/xmonad.hs /src/xmonad.hs
# xmonad tries to recompile and re-exec itself unless the binary has this exact name
RUN cd /src && ghc --make -O2 -threaded -outputdir /tmp/ghc xmonad.hs -o /src/xmonad-x86_64-linux


FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=Asia/Seoul

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gpg fontconfig fonts-dejavu-core tzdata procps xauth \
      openbox tigervnc-standalone-server tigervnc-tools tigervnc-viewer \
      xtightvncviewer xinput x11-xserver-utils x11-apps x11-utils xterm tint2 dbus-x11 \
      python3-minimal rofi libxss1 xwallpaper \
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

COPY --from=xmonad-build /src/xmonad-x86_64-linux /usr/local/bin/xmonad-x86_64-linux
COPY rootfs/ /

USER vdesk
WORKDIR /home/vdesk
ENTRYPOINT ["/usr/local/bin/vdesk-entrypoint"]
