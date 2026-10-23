# 설치와 준비

Raven은 서버(Linux 호스트) 한 대와 클라이언트(X 서버가 있는 PC)로 동작한다. 서버에서 세션 컨테이너가 돌고, 클라이언트에는 뷰어 창만 뜬다. 사용법은 [usage.md](./usage.md).

```
client (Windows + VcXsrv)          server (Linux + Docker)
┌─────────────────────────┐        ┌──────────────────────────────────┐
│ X server  <── viewer ───┼── X11 ─┼── container raven-<user>-<id>    │
│ SSH client (ssh -Y)     │  (SSH) │     Xvnc + WM + apps + viewer    │
└─────────────────────────┘        │ raven CLI, raven-hostd (relay)   │
                                   └──────────────────────────────────┘
```

- 뷰어(`xtigervncviewer`)는 서버의 컨테이너 안에서 실행되고, SSH X11 forwarding으로 클라이언트 X 서버에 창을 띄운다. 클라이언트에 VNC 뷰어를 설치하지 않는다.
- 서버에는 Docker와 몇 가지 기본 도구만 있으면 된다. 앱은 모두 이미지 안에 들어간다.

## 확인한 환경

| 항목            | 확인한 것                                                            |
| --------------- | -------------------------------------------------------------------- |
| 서버 OS         | Ubuntu 24.04, 커널 6.8                                               |
| Docker          | Ubuntu `docker.io` 29.1.3 + `docker-buildx`, rootful                 |
| 서버 홈         | NFSv3 (`sec=sys`)                                                    |
| 클라이언트      | Windows + VcXsrv 1.20.14 (한국어 자판), Xming 6.9                    |
| 클라이언트 접속 | SSH X11 forwarding, VcXsrv에 TCP로 직접 연결                         |

macOS(XQuartz)와 Linux 데스크톱 클라이언트, rootless Docker, 다른 배포판은 확인하지 않았다.

## 서버 준비

### 패키지

| 필요한 것                                 | 용도                                                               | Ubuntu 패키지                         |
| ----------------------------------------- | ------------------------------------------------------------------ | ------------------------------------- |
| Docker Engine (rootful)                   | 세션 컨테이너, 이미지 빌드                                         | `docker.io` 또는 Docker의 `docker-ce` |
| BuildKit (`docker buildx`)                | addon을 이름 붙인 build context로 넣는 빌드                        | `docker-buildx`                       |
| `python3`                                 | 호스트 중계 `raven-hostd`                                          | `python3`                             |
| `xauth`                                   | X 쿠키를 뷰어와 `run --host` 앱에 넘김. sshd X11 forwarding도 필요 | `xauth`                               |
| `ss`                                      | 비어 있는 화면 번호, VNC 포트, 중계 확인                           | `iproute2`                            |
| GNU coreutils, tar, findutils, util-linux | CLI 스크립트 (`sha256sum`, `tar --sort`, `find -print0`, `setsid`) | 기본 설치                             |
| sshd                                      | X11 forwarding                                                     | `openssh-server`                      |

이 밖에 VNC, 창 관리자, 브라우저 같은 패키지는 서버에 설치하지 않는다.

### 사용자와 Docker 권한

- Raven을 쓰는 사용자는 Docker 데몬을 쓸 수 있어야 한다(`docker` 그룹). `docker` 그룹은 사실상 root 권한이므로 믿을 수 있는 사용자에게만 준다.
- 세션 안의 사용자는 uid/gid 60000이다. 서버에 uid 60000인 계정이 있으면 그 계정과 파일 소유가 겹친다. `getent passwd 60000`으로 비어 있는지 확인한다.
- 각 사용자의 기본 그룹은 그 사용자만 속한 개인 그룹이어야 한다(Ubuntu 기본값). 세션은 이 그룹으로 `~/Downloads`를 공유한다. 여러 사람이 같이 쓰는 그룹이 기본 그룹이면 그 사람들도 `~/Downloads`에 쓸 수 있게 된다.

### sshd

`/etc/ssh/sshd_config`에서 X11 forwarding을 켠다. Ubuntu 기본값은 켜져 있다.

```
X11Forwarding yes
X11UseLocalhost yes
```

접속한 셸에서 `echo $DISPLAY`가 `localhost:10.0` 같은 값이면 된다.

### 홈 디렉터리

- 세션 홈은 Docker named volume(`/var/lib/docker/volumes`)에 있으므로 사용자 홈이 NFS여도 된다. Edge 같은 Chromium 계열 프로필의 SQLite 잠금 문제를 피하려고 홈 디렉터리를 세션 홈으로 쓰지 않는다.
- `~/Downloads`는 세션에 그대로 연결된다. NFS면 서버가 클라이언트가 보낸 그룹 목록을 인정해야 세션이 쓸 수 있다(`sec=sys` 기본 동작). NFS 서버가 그룹을 자기 쪽에서 다시 찾는 설정(`rpc.mountd --manage-gids`)이면 uid 60000에 그룹이 없어 쓰기가 막힐 수 있다(확인하지 않음).

### 자원

| 항목                 | 크기 (실측)                                          |
| -------------------- | ---------------------------------------------------- |
| core 이미지          | 약 1.05GB                                            |
| core + edge + kime   | 약 2.1GB                                             |
| 세션 홈 volume       | 수백 kB ~ 1.5GB (Edge 프로필과 캐시에 따라)          |
| 세션 메모리          | 약 80MB(xterm만) ~ 600MB(Edge)                       |

이미지 태그(`RAVEN_IMAGE`)를 사용자별로 나누면 그만큼 이미지가 더 생긴다. 앞쪽 apt 레이어는 공유된다.

### 네트워크

- 빌드할 때 인터넷이 필요하다: Docker Hub(`ubuntu:24.04`), Ubuntu apt 저장소, GitHub(Noto Sans KR 글꼴, kime `.deb`), packages.microsoft.com(Edge).
- 실행 중에는 바깥에 여는 포트가 없다. VNC는 `127.0.0.1:15900`부터, X 화면은 host network namespace의 abstract socket(`@/tmp/.X11-unix/X100`부터)이고 MIT 쿠키를 건다. 방화벽 설정은 필요 없다.

### 지역 설정

- 시간대: 세션은 서버의 시간대를 따른다. Raven이 세션 컨테이너를 만들 때(`new`, `recreate`) `TZ`를 넘긴다. 셸의 `TZ`가 있으면 그것, 없으면 `/etc/localtime`이 가리키는 시간대다. 서버 시간대를 바꾼 뒤에는 `recreate`해야 세션에 반영된다.
- 로캘 `LANG=C.UTF-8`, 글꼴 DejaVu와 Noto Sans KR(한글)은 이미지에 고정되어 있다. 바꾸려면 `Dockerfile`을 고치고 `raven build` 후 `recreate`한다.

### 재부팅

세션 컨테이너는 `--restart unless-stopped`로 만들어진다. Docker 서비스가 부팅 때 뜨게 되어 있으면(`systemctl is-enabled docker`) 재부팅 뒤 세션도 다시 뜬다. 세션 안의 앱은 다시 시작된다.

## 클라이언트 준비

### Windows

1. VcXsrv를 설치하고 XLaunch로 띄운다.
   - Display settings: Multiple windows, Display number `0`
   - Client startup: Start no client
   - Extra settings: Clipboard 켜기. SSH X11 forwarding으로 쓸 때는 Disable access control을 끈 채로 둔다.
2. SSH 클라이언트에서 X11 forwarding을 켜고 서버에 접속한다.
   - OpenSSH: `set DISPLAY=127.0.0.1:0.0`(cmd) 또는 `$env:DISPLAY="127.0.0.1:0.0"`(PowerShell) 뒤 `ssh -Y user@server`
   - PuTTY: Connection > SSH > X11 > Enable X11 forwarding
3. 서버 셸에서 `echo $DISPLAY`를 확인하고 `raven`으로 첫 세션을 만든다.

VcXsrv에 TCP로 직접 붙이는 방법(서버에서 `DISPLAY=<클라이언트 IP>:0`)도 동작하지만, VcXsrv의 접근 제어를 꺼야 해서 같은 네트워크의 누구나 그 X 서버에 붙을 수 있게 된다. SSH X11 forwarding을 권한다.

Xming 6.9처럼 XInput 2가 없는 X 서버에서는 `xtightvncviewer`로 붙고 창 크기가 고정된다([usage.md](./usage.md#뷰어)).

### 키보드 (Windows + VcXsrv)

- Windows가 Win 키와 `Alt+Tab`을 가로챈다. 그래서 xmonad 조작 키는 Alt이고, 창 이동은 `Alt+j`/`Alt+k`다.
- 한국어 자판의 한/영 키와 한자 키는 VcXsrv가 왼쪽 Alt와 왼쪽 Ctrl로 보낸다. 세션에서는 이 키로 한/영을 바꿀 수 없다. `kime` addon의 기본 설정은 Shift+Space로 바꾸고, 작업표시줄의 `한/A` 버튼도 있다([usage.md](./usage.md#한글-입력)).
- Windows IME는 영문 상태로 둔다.

### macOS, Linux

확인하지 않았다. X 서버(macOS는 XQuartz)와 `ssh -Y`가 있으면 같은 방식으로 붙을 것으로 본다. 뷰어는 X 서버에 XInput 2가 있으면 `xtigervncviewer`, 없으면 `xtightvncviewer`를 고른다.

## 설치

```bash
git clone <저장소> ~/raven
mkdir -p ~/.local/bin
ln -s ~/raven/raven ~/.local/bin/raven     # ~/.local/bin이 PATH에 있어야 한다
raven addon enable kime                    # 선택: 한글 입력 (기본으로 켜진 것은 edge)
raven build                                # 처음에는 xmonad 컴파일과 Edge 내려받기로 오래 걸린다
```

빌드는 core 이미지 `raven-core:latest`를 만든 뒤 켜진 addon을 레이어로 얹어 `raven:latest`를 만든다. 이미지 이름은 `RAVEN_IMAGE`, `RAVEN_CORE_IMAGE`로 바꾼다.

## 첫 실행 확인

```bash
raven doctor               # 서버 준비 상태. fail이 없어야 한다
raven new -s t xclock      # 세션을 만들고 뷰어를 띄운다. 시계가 보이면 된다
raven ls                   # STATE running, VIEWERS 1
raven menu show t          # 메뉴 항목
raven kill t
```

- `DISPLAY is not set`: X11 forwarding 없이 접속했다.
- `image raven:latest not found`: `raven build`를 먼저 한다.
- `session N did not come up`: `docker logs raven-<user>-N`.

`raven doctor`가 보는 것은 다음과 같다. 결과는 `ok`, `info`, `warn`, `fail`로 나오고, `fail`이 있으면 종료 코드가 1이다.

| 항목             | 확인 내용                                                                  |
| ---------------- | -------------------------------------------------------------------------- |
| Docker, buildx   | 데몬에 닿는지(권한), BuildKit이 있는지                                     |
| 호스트 도구      | `python3`, `xauth`, `ss`, GNU `tar` 등                                     |
| 이미지           | `raven build`를 했는지, 세션 uid 분리 이후 이미지인지, 켜진 addon          |
| 세션 uid, 그룹   | uid 60000이 서버 계정과 겹치지 않는지, 기본 그룹을 다른 사람이 같이 쓰는지 |
| `~/Downloads`    | 그룹 쓰기와 setgid가 켜졌는지                                              |
| 세션             | 이전 이미지로 만든 세션(`recreate` 대상)                                   |
| 호스트 중계      | 이 Raven의 중계인지, 중계 파일이 바뀐 뒤에 뜬 것인지                       |
| `DISPLAY`        | 설정됐는지, X 쿠키가 있는지                                                |
| 시간대, 부팅     | 세션에 넘길 시간대, Docker가 부팅 때 뜨는지                                |

## 여러 사용자가 한 서버를 쓸 때

- 세션 컨테이너, 홈 volume, 설정, 상태는 사용자별로 나뉜다(`raven-<user>-*`, 각자의 `~/.config/raven`).
- 이미지 태그는 Docker 데몬 하나를 모두가 같이 쓴다. 누가 `raven build`를 하면 `raven:latest`는 그 사람의 addon 구성으로 바뀐다. addon 구성이 다르면 사용자별로 `RAVEN_IMAGE=raven-$USER:latest`, `RAVEN_CORE_IMAGE=raven-core-$USER:latest`를 셸 설정에 둔다.
- 세션 사용자 uid(60000)는 모두 같다. 호스트 중계는 uid가 아니라 요청한 프로세스의 컨테이너 label로 사용자와 세션을 확인한다.
- 화면 번호와 VNC 포트는 모든 사용자의 세션 중 비어 있는 것을 고른다.

## Raven이 만들고 바꾸는 것

Raven이 서버에 남기는 것은 아래가 전부다. `/etc` 같은 시스템 설정은 바꾸지 않는다.

| 위치                               | 내용                                                                    | 만드는 때                                 |
| ---------------------------------- | ----------------------------------------------------------------------- | ----------------------------------------- |
| `~/.config/raven/`                 | `menu.conf`, `wallpaper.*`, `addons.enabled`, `addons/`, `home/`        | `menu edit`, `wallpaper`, `addon`, 사용자 |
| `~/.local/state/raven/<hostname>/` | 세션 이름(`<id>.name`), 개인 설정 복사 기록(`<id>.home`)                | `new -s`, `rename`, 세션을 다룰 때        |
| `$XDG_RUNTIME_DIR/raven-<uid>/`    | `run --host` 쿠키와 로그, 중계 로그 (`XDG_RUNTIME_DIR`가 없으면 `/tmp`) | `run --host`, 중계 시작                   |
| `~/Downloads`                      | 없으면 만든다. 그룹 쓰기와 setgid를 켠다(`chmod g+rwxs`)                | 세션 컨테이너를 만들 때                   |
| Docker 이미지                      | `raven-core:latest`, `raven:latest`(또는 `RAVEN_*_IMAGE`), 빌드 캐시    | `build`                                   |
| Docker 컨테이너                    | `raven-<user>-<id>`. 홈 이전과 멈춘 세션 복사에 잠깐 쓰는 `--rm` 도우미 | `new`, `recreate`, 세션을 다룰 때         |
| Docker volume                      | `raven-<user>-home-<id>`                                                | `new`                                     |
| 호스트 프로세스                    | `raven-hostd` (같은 사용자로 실행, `raven hostd stop`으로 끝낸다)       | 세션을 다룰 때                            |
| 소켓, 포트                         | 중계 `@raven-host-<uid>`, 세션의 X 화면과 `127.0.0.1:159xx` VNC 포트    | 실행 중                                   |
| `~/.local/bin/raven`               | CLI symlink                                                             | 설치할 때 사용자가 만든다                 |

Raven이 읽기만 하는 바깥 파일은 다음과 같다.

- `$DISPLAY`의 X 쿠키(`xauth`가 `~/.Xauthority` 또는 `$XAUTHORITY`에서 읽는다). 뷰어에 넘긴다.
- `~/.config/raven/home/` 안의 symlink가 가리키는 파일. 따라가서 내용을 세션에 복사한다.
- `raven wallpaper set`에 준 그림 파일. `~/.config/raven/`에 복사해 둔다.
- `menu edit`의 `$VISUAL`, `$EDITOR`.
- `menu.conf`의 `@` 항목은 호스트 명령이다. 그 명령(예: 호스트 wezterm)이 자기 설정을 읽는 것은 Raven과 무관하다.

## 제거

```bash
raven uninstall -n                     # 지울 것만 보여 준다
raven uninstall                        # 확인('yes' 입력) 후 지운다
raven uninstall --config --images      # 설정과 이미지까지
```

- 기본으로 지우는 것: 이 사용자의 세션 컨테이너와 홈 volume(컨테이너 없이 남은 volume 포함), 이 서버의 상태 디렉터리(`~/.local/state/raven/<hostname>`), runtime 디렉터리, 호스트 중계.
- `--config`: `~/.config/raven`(메뉴, 배경, addon, 개인 설정 파일).
- `--images`: `RAVEN_IMAGE`, `RAVEN_CORE_IMAGE`. 다른 세션(다른 사용자 포함)이 쓰는 이미지는 남긴다.
- `-y`는 확인 없이 지운다. 터미널이 아닌 곳에서 `-y` 없이 실행하면 멈춘다.
- 지우지 않는 것: `~/Downloads`와 그 안의 파일, Raven 저장소, `~/.local/bin/raven`. 손으로 마무리한다.

```bash
rm ~/.local/bin/raven
chmod g-ws ~/Downloads                 # Raven이 켠 그룹 공유 설정을 되돌릴 때
```
