# M11 checkpoint lab: a privileged Docker-in-Docker container whose own dockerd runs with
# experimental features and CRIU, so the shared server's Docker and packages stay as they are.
# Docker, containerd and runc are the Ubuntu 24.04 packages the server runs; CRIU is not in the
# Ubuntu 24.04 archive and is built from a pinned upstream tag.

FROM ubuntu:24.04 AS criu-build

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential ca-certificates git pkg-config python3 \
      libprotobuf-dev libprotobuf-c-dev protobuf-c-compiler protobuf-compiler python3-protobuf \
      libnl-3-dev libnet1-dev libcap-dev libbsd-dev libnftables-dev libgnutls28-dev uuid-dev libaio-dev \
 && rm -rf /var/lib/apt/lists/*

ARG CRIU_TAG=v4.3.1
ARG CRIU_COMMIT=3ce556b7e8291c1bfdca974962f6282d7802b69f
RUN git clone --depth 1 --branch "$CRIU_TAG" https://github.com/checkpoint-restore/criu /src/criu \
 && cd /src/criu \
 && test "$(git rev-parse HEAD)" = "$CRIU_COMMIT" \
 && make -j"$(nproc)" criu \
 && install -D -m 0755 criu/criu /out/usr/sbin/criu


FROM ubuntu:24.04

# The server's versions (Ubuntu packages): docker.io 29.1.3, containerd 2.2.1, runc 1.3.4
ARG DOCKER_VERSION=29.1.3-0ubuntu3~24.04.2
ARG CONTAINERD_VERSION=2.2.1-0ubuntu1~24.04.3
ARG RUNC_VERSION=1.3.4-0ubuntu1~24.04.1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      docker.io="$DOCKER_VERSION" containerd="$CONTAINERD_VERSION" runc="$RUNC_VERSION" \
      iptables iproute2 nftables procps psmisc kmod ca-certificates python3 python3-cryptography \
      libprotobuf-c1 libnl-3-200 libnet1 libcap2 libbsd0 libgnutls30t64 libnftables1 libuuid1 libaio1t64 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=criu-build /out/ /
RUN ! ldd /usr/sbin/criu | grep 'not found' && criu --version

# The raven CLI runs as an ordinary user in the docker group, as on the server
RUN userdel -r ubuntu 2>/dev/null || true; useradd -m -u 1000 -U -G docker lab

COPY daemon.json /etc/docker/daemon.json
COPY runc.conf /etc/criu/runc.conf.base
COPY apparmor_parser /usr/local/lib/raven-lab/apparmor_parser
COPY lab-entrypoint /usr/local/bin/lab-entrypoint

LABEL raven.lab=m11
VOLUME /var/lib/docker
ENTRYPOINT ["/usr/local/bin/lab-entrypoint"]
