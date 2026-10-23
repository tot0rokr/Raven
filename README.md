# Raven

Remote Access to Virtual Environment Nodes. tmux처럼 쓰는 지속형 X 데스크톱. 세션마다 Docker 컨테이너 안에 가상 화면(TigerVNC `Xvnc`)과 창 관리자(xmonad 또는 openbox)를 띄우고, 뷰어를 X11(`ssh -Y`)로 클라이언트 X 서버에 띄운다. 뷰어를 닫거나 SSH가 끊겨도 세션 안의 앱은 계속 실행되고, 다시 attach하면 그대로 이어서 쓴다. 앱은 addon으로 이미지에 추가하고, 서버에는 Docker와 기본 도구 외에 설치하지 않는다. 클라이언트에는 X 서버와 SSH 클라이언트만 있으면 된다.

## 기능

- 세션: 번호(0부터)와 이름으로 관리하고 attach/detach한다. 한 세션에 뷰어 여러 개가 붙을 수 있고, VM 재부팅 뒤에는 자동으로 다시 뜬다.
- 데스크톱: 창 관리자는 xmonad(기본, 타일링) 또는 openbox. tint2 작업표시줄과 시계, rofi 메뉴, 세션 안에서 보는 단축키 목록(`Alt+Shift+/`), D-Bus 세션 버스가 있다.
- 앱: addon으로 이미지에 넣는다. 기본으로 Microsoft Edge가 켜져 있다. 메뉴는 설치된 앱의 `.desktop`에서 자동으로 만들고 `menu.conf`로 고친다.
- 호스트 앱: 호스트에 설치된 GUI 앱(wezterm 등)을 세션 화면에 띄우고, 세션 메뉴에서도 실행한다.
- 배경화면: 모든 세션 또는 세션별로 이미지나 단색을 지정한다.
- 한글 입력: `kime` addon으로 세션 안에서 한글을 입력한다 (Edge, xterm). 기본은 두벌식이고, VcXsrv가 한/영 키를 왼쪽 Alt로 보내므로 Shift+Space나 작업표시줄의 `한/A` 버튼으로 전환한다. 개인 설정으로 다른 자판과 키를 쓴다.
- 개인 설정: `~/.config/raven/home/`에 둔 파일(dotfiles symlink 포함)을 모든 세션 홈에 복사한다.
- 다운로드: 세션의 `~/Downloads`가 호스트 `~/Downloads`다.
- 구형 X 서버: XInput 2가 없는 Xming 6.9에서도 attach한다.

## 요구 사항

- 서버: Linux, rootful Docker와 BuildKit(`docker buildx`), `python3`, `xauth`, `ss`, X11 forwarding이 켜진 sshd. 사용자는 `docker` 그룹이어야 한다. 빌드할 때 인터넷이 필요하다.
- 클라이언트: X 서버(Windows는 VcXsrv 권장, Xming 6.9도 됨), X11 forwarding을 켠 SSH 클라이언트.
- 확인한 환경은 Ubuntu 24.04 서버와 Windows + VcXsrv 클라이언트다. 준비 과정, 여러 사용자, Raven이 서버에 만드는 것, 제거 방법은 [docs/install.md](./docs/install.md).

## 설치

```bash
git clone <저장소> ~/raven
ln -s ~/raven/raven ~/.local/bin/raven
raven doctor                           # 서버 준비 상태 점검
raven build
```

지울 때는 `raven uninstall`([docs/install.md](./docs/install.md#제거)).

`raven build`는 core 이미지(`raven-core:latest`)를 만든 뒤 켜진 addon을 하나씩 레이어로 얹어 `raven:latest`를 만든다. 처음 빌드는 xmonad 설정 컴파일과 Edge 내려받기 때문에 오래 걸린다.

## 빠른 시작

```bash
ssh -Y user@vm                         # 클라이언트에서 X11 forwarding으로 접속
raven new -s web microsoft-edge        # Edge 세션을 만들고 화면을 띄운다
                                       # 뷰어 창을 닫거나 SSH가 끊겨도 Edge는 계속 실행된다
raven a web                            # 다시 attach
raven ls                               # 세션 목록
raven kill web                         # 세션과 홈 삭제
```

xmonad 세션에서는 `Alt+p`가 메뉴, `Alt+Shift+Enter`가 터미널, `Alt+j`/`Alt+k`가 창 이동이다. 전체 키와 명령은 [docs/usage.md](./docs/usage.md).

## 문서

| 문서                                 | 내용                                                  |
| ------------------------------------ | ----------------------------------------------------- |
| [docs/install.md](./docs/install.md) | 서버와 클라이언트 준비, 설치, 여러 사용자, 제거       |
| [docs/usage.md](./docs/usage.md)     | 명령별 사용법, 키, 데이터 위치, 환경 변수, 문제 해결  |
| [docs/menu.md](./docs/menu.md)       | 메뉴 규칙 문법과 우선순위                             |
| [docs/addons.md](./docs/addons.md)   | addon 작성법                                          |
| [CLAUDE.md](./CLAUDE.md)             | 개발 지침: 파일 구성, 규칙, 검증 순서                 |

## 구조

```
image raven:latest = raven-core (Xvnc, xmonad, openbox, tint2, rofi, viewers, xterm) + enabled addons (edge, ...)

┌─ session 0 (:100, 15900) ──────┐   ┌─ session 1 (:101, 15901) ──────┐
│ Xvnc + xmonad                  │   │ Xvnc + openbox                 │
│  ├ microsoft-edge (addon)      │   │  └ xterm (core)                │
│  └ wezterm (host, run --host)  │   │                                │
│ volume: raven-<user>-home-0    │   │ volume: raven-<user>-home-1    │
└───────────────┬────────────────┘   └────────────────┬───────────────┘
                └──── viewer ── X11 (ssh -Y) ─────────┴──> VcXsrv / Xming
```

- 세션 하나는 컨테이너 `raven-<user>-<id>`와 홈 volume `raven-<user>-home-<id>`다.
- 뷰어(`xtigervncviewer` 또는 `xtightvncviewer`)도 컨테이너 안에서 실행되고, SSH X11 forwarding으로 클라이언트 X 서버에 창을 띄운다. 그래서 클라이언트에는 X 서버만 있으면 되고 VNC 뷰어를 따로 설치하지 않는다.
- 호스트 앱은 호스트 프로세스가 세션의 X socket에 직접 붙는다. 세션 메뉴의 호스트 항목은 호스트 중계 `raven-hostd`를 거친다.
- CLI(`raven`)는 POSIX sh 스크립트 하나다. 세션 정보는 컨테이너 label과 `~/.local/state/raven/<hostname>/`에 둔다.

## 보안 설계

- `--network host`: 뷰어가 SSH X11 forwarding 포트(`127.0.0.1:60xx`)에 닿고, `run --host` 앱이 세션의 X socket(abstract socket)에 닿기 위해 쓴다.
- 화면 번호는 100부터, VNC 포트는 15900부터 비어 있는 것을 고른다. VNC는 `127.0.0.1`에만 열리고, 비밀번호는 세션이 시작할 때마다 새로 만들어 컨테이너 밖으로 꺼내지 않는다.
- X 화면은 host network namespace에 노출되므로 MIT-MAGIC-COOKIE 인증을 건다.
- Xvnc의 MIT-SHM 확장을 끈다. `run --host` 앱은 컨테이너와 IPC namespace가 달라 X 서버와 SysV 공유 메모리를 나눌 수 없고, Mesa(OpenGL) 앱은 이때 아무것도 그리지 못한다. 호스트 IPC를 공유(`--ipc host`)하면 해결되지만 컨테이너가 호스트 사용자의 공유 메모리에 접근할 수 있게 되므로 쓰지 않는다.
- 세션 사용자는 호스트 계정과 다른 uid 60000이다. 컨테이너가 host network namespace에 있어도, uid로 상대를 확인하는 호스트 사용자의 소켓을 통과하지 못한다. 호스트와는 `~/Downloads`만 호스트 사용자의 그룹으로 공유한다.
- 세션과 호스트 사이에는 메뉴 라벨만 오간다. 호스트 중계는 요청한 프로세스가 그 사용자의 그 세션 컨테이너 안에 있을 때만 받고, 명령은 호스트의 `menu.conf`에서만 찾는다.
- seccomp, `/dev/shm` 크기는 addon이 요구할 때만 바꾼다. 켜진 addon 중 하나라도 `SECCOMP=unconfined`면 모든 세션에 적용된다 (`edge`가 Chromium sandbox 때문에 요구). `--no-sandbox`는 쓰지 않는다.

## 라이선스

MIT. [LICENSE](./LICENSE).
