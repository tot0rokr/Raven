# vdesk

tmux처럼 쓰는 지속형 X 데스크톱. 세션마다 Docker 컨테이너 안에 가상 화면(TigerVNC `Xvnc`)과 창 관리자(xmonad 또는 openbox)를 띄우고, 뷰어를 X11(`ssh -Y`)로 클라이언트 X 서버에 띄운다. 뷰어를 닫거나 SSH가 끊겨도 세션 안의 앱은 계속 실행된다. 앱은 addon으로 이미지에 추가하고, 호스트에는 Docker 외 패키지를 설치하지 않는다.

```
image vdesk:latest = vdesk-core (Xvnc, xmonad, openbox, tint2, rofi, viewers, xterm) + enabled addons (edge, ...)

┌─ session 0 (:100, 15900) ──────┐   ┌─ session 1 (:101, 15901) ──────┐
│ Xvnc + xmonad                  │   │ Xvnc + openbox                 │
│  ├ microsoft-edge (addon)      │   │  ├ xterm (core)                │
│  └ xeyes (host, run --host)    │   │  └ gtkwave (addon)             │
│ volume: vdesk-<user>-home-0    │   │ volume: vdesk-<user>-home-1    │
└───────────────┬────────────────┘   └────────────────┬───────────────┘
                └──── viewer ── X11 (ssh -Y) ─────────┴──> VcXsrv / Xming
```

## 설치

```bash
ln -s ~/vdesk/vdesk ~/.local/bin/vdesk
vdesk build
```

`vdesk build`는 core 이미지(`vdesk-core:latest`)를 만든 뒤 켜진 addon을 하나씩 레이어로 얹어 `vdesk:latest`를 만든다. 기본으로 켜진 addon은 `edge`다.

## 세션

```bash
vdesk                                  # 새 세션(xmonad, xterm) 생성 후 attach
vdesk new -s edge microsoft-edge       # 이름과 시작 명령 지정
vdesk new -w openbox -s ob             # 창 관리자를 openbox로
vdesk new -d -s sim xclock             # 만들기만 하고 attach하지 않음
vdesk ls                               # 세션 목록
vdesk a edge                           # 이름으로 attach
vdesk a -g 1920x1000 0                 # id로 attach, 가상 화면 크기 지정
vdesk a -d 0                           # 다른 뷰어를 끊고 attach
vdesk rename 0 browser                 # 이름 변경
vdesk run -t edge xclock               # 세션 안(컨테이너)에서 앱 실행
vdesk run -t 0 --host gtkwave a.vcd    # 호스트 앱을 세션 화면에 실행
vdesk shell -t 0                       # 세션 안의 bash (DISPLAY 설정됨)
vdesk kill sim                         # 세션과 홈 volume 삭제
```

- TARGET은 세션 id 또는 이름이다. 생략하면 가장 최근에 만든 세션을 쓴다. `kill`은 TARGET이 필수다.
- id는 0부터 비어 있는 가장 작은 번호를 쓴다. 이름은 영문자로 시작해야 하고 세션 간에 겹칠 수 없다.
- 옵션은 TARGET 앞에 둔다 (`vdesk a -g 1920x1000 0`).
- 뷰어 창을 닫으면 detach다. 세션은 VM 재부팅 뒤에도 다시 뜨고, 시작 명령(`new`의 CMD)도 다시 실행된다.
- 재부팅 뒤 추가로 띄울 앱은 세션 홈에 적는다 (`vdesk shell`로 편집). xmonad 세션은 `~/.config/vdesk/autostart`, openbox 세션은 `~/.config/openbox/autostart`이고, 둘 다 `app &` 형식의 셸 스크립트다.

## 창 관리자

세션마다 `vdesk new -w xmonad|openbox`로 고른다. 기본은 xmonad이고, `VDESK_WM` 환경 변수로 기본값을 바꿀 수 있다. `vdesk ls`의 WM 열에 표시되고, `recreate`해도 유지된다. 창 관리자를 고를 수 있기 전에 만든 세션은 openbox다.

### xmonad

타일링 창 관리자다. 새 창이 화면을 나눠 갖고, 대화상자와 오류 창은 가운데에 떠 있는 창으로 뜬다. 설정(`xmonad/xmonad.hs`)은 이미지 빌드 단계에서 컴파일되어 들어가므로, 바꾸려면 파일을 고치고 `vdesk build` 후 새 세션을 만든다.

Windows가 Win 키를 가로채므로 조작 키(mod)는 Alt다.

| 키                        | 동작                                                   |
| ------------------------- | ------------------------------------------------------ |
| `Alt+p`                   | 메뉴 (rofi, 작업표시줄의 `menu` 버튼과 같음)           |
| `Alt+Shift+Enter`         | xterm                                                  |
| `Alt+j` / `Alt+k`         | 다음 / 이전 창으로 포커스                              |
| `Alt+Enter`               | 포커스된 창을 주 영역으로                              |
| `Alt+h` / `Alt+l`         | 주 영역 줄이기 / 늘리기                                |
| `Alt+\`, `Alt+Space`      | 레이아웃 전환 (좌우 분할 → 상하 분할 → 전체 화면)      |
| `Alt+1`..`9`              | 작업 공간 이동                                         |
| `Alt+Shift+1`..`9`        | 포커스된 창을 작업 공간으로 보냄                       |
| `Alt+t`                   | 떠 있는 창을 다시 타일로                               |
| `Alt+왼쪽 드래그` / `Alt+오른쪽 드래그` | 창을 띄워서 이동 / 크기 조절             |
| `Alt+Shift+c`             | 포커스된 창 닫기                                       |
| `Alt+q`                   | xmonad 재시작 (창은 유지)                              |

- `Alt+Tab`, `Alt+Space`, `Alt+F4`는 Windows가 먼저 가로챌 수 있다 (추정). 레이아웃 전환은 `Alt+\`를 쓴다.
- 작업표시줄에는 작업 공간 1~9가 묶음으로 나오고, 현재 작업 공간이 강조된다. 창 버튼을 누르면 그 창으로 이동하고, 가운데 버튼은 창을 닫는다.
- 메뉴 항목은 openbox 세션의 우클릭 메뉴와 같은 `menu.conf`와 `.desktop` 파일에서 나온다 ([docs/menu.md](./docs/menu.md)).

### openbox

바탕화면 우클릭 메뉴와 창 제목 표시줄이 있는 일반 데스크톱이다. 작업표시줄 버튼으로 최소화와 복원을 한다.

## 호스트 앱

호스트에 설치된 GUI 앱을 세션 화면에 띄울 수 있다.

```bash
vdesk run -t edge --host wezterm start --always-new-process   # 명령을 직접 지정
vdesk run -t edge @term                                       # menu.conf의 호스트 항목 라벨로
```

- 호스트 프로세스라 격리되지 않는다. 호스트의 홈, 설정, 셸 환경을 그대로 쓴다.
- SSH가 끊겨도 세션 화면에 남는다. 세션을 `kill`하거나 `recreate`하면 화면이 사라지면서 함께 종료된다.
- 출력과 오류는 `$XDG_RUNTIME_DIR/vdesk-<uid>/<id>.log`에 쌓인다.

### 메뉴에 넣기

`vdesk menu edit`으로 `menu.conf`를 열고, 명령 앞에 `@`를 붙인 항목을 적는다.

```
term = @wezterm start --always-new-process
```

- 세션의 우클릭 메뉴에 `term`이 생기고, 누르면 호스트에서 실행된다. 명령줄에서는 `vdesk run -t TARGET @term`(라벨에 공백이 있으면 `@"Host shell"`).
- 메뉴 항목은 컨테이너 안에서 실행되므로, 호스트 중계 프로세스 `vdesk-hostd`에 요청을 보내 호스트에서 실행한다. 세션은 라벨만 보내고, 명령은 호스트의 `~/.config/vdesk/menu.conf`에서만 찾는다. 같은 사용자의 요청만 받는다.
- 그래서 `@` 항목은 사용자의 `menu.conf`에서만 동작한다. 이미지 기본 규칙(`/etc/vdesk/menu.d`)에 넣으면 메뉴에는 나오지만 실행되지 않는다.
- 중계 프로세스는 세션을 다루는 명령(`new`, `attach`, `run`, `shell`, `recreate`)이 실행될 때 없으면 뜬다. VM 재부팅 뒤 메뉴에서 호스트 앱이 안 뜨면 `vdesk hostd start`를 실행한다. 실패하면 세션 화면에 오류 창이 뜬다.
- `vdesk hostd status | start | stop`으로 관리한다. 로그는 `$XDG_RUNTIME_DIR/vdesk-<uid>/hostd.log`.
- 이 기능 이후 이미지로 만든 세션에서만 메뉴에서 실행된다.

### wezterm

- 호스트의 `~/.wezterm.lua`를 그대로 쓴다. GPU 없이 Mesa llvmpipe로 그리므로 호스트에 `libegl1`, `libegl-mesa0`이 있어야 한다.
- `--always-new-process`: 다른 화면에서 이미 실행 중인 wezterm이 있으면, 그 프로세스에 새 창이 열려 엉뚱한 화면에 뜨는 것을 막는다.
- MIT-SHM을 끈 뒤(아래 보안 설계)에 만든 세션에서만 그려진다. 그 전 세션에서는 창이 검게 나온다. `vdesk recreate`로 옮기거나 새 세션에서 띄운다.

## 앱 추가 (addon)

```bash
vdesk addon ls                         # addon 목록, 켜짐/빌드 여부
vdesk addon new gtkwave gtkwave        # apt 패키지만 쓰는 addon 생성
vdesk addon enable gtkwave             # 켜기 (disable로 끄기)
vdesk build                            # 이미지 재빌드
vdesk recreate -a                      # 기존 세션을 새 이미지로 교체 (홈 유지)
```

- 기본 제공 addon은 `~/vdesk/addons/`, 사용자 addon은 `~/.config/vdesk/addons/`에 둔다. 이름이 같으면 사용자 addon이 우선한다.
- 켜진 목록은 `~/.config/vdesk/addons.enabled`다. 파일이 없으면 `edge`만 켜진 것으로 본다.
- 저장소 등록, 설정 파일, 실행 옵션 고정 등 addon 작성법은 [docs/addons.md](./docs/addons.md).

## 메뉴

xmonad 세션은 `Alt+p`나 작업표시줄의 `menu` 버튼, openbox 세션은 바탕화면 우클릭으로 메뉴를 연다. 메뉴는 열 때마다 규칙 파일과 설치된 앱의 `.desktop` 파일로 새로 만들어진다.

```bash
vdesk menu edit                        # ~/.config/vdesk/menu.conf 편집 후 모든 세션에 반영
vdesk menu show -t 0                   # 세션 0의 실제 메뉴 출력
```

```
# ~/.config/vdesk/menu.conf
Edge (work) = microsoft-edge --profile-directory=Work
!Microsoft Edge
```

규칙 문법과 우선순위는 [docs/menu.md](./docs/menu.md).

## 작업표시줄

세션 화면 아래쪽에 [tint2](https://gitlab.com/o9000/tint2) 작업표시줄이 있다. 오른쪽은 날짜와 시각이다.

- xmonad 세션: 왼쪽부터 `menu` 버튼, 작업 공간별 창 목록이다. 기본 설정은 이미지의 `/etc/vdesk/tint2/xmonad.tint2rc`.
- openbox 세션: 열린 창 목록이고, 창 버튼을 누르면 최소화와 복원이 바뀐다. 기본 설정은 이미지의 `/etc/xdg/tint2/tint2rc`.
- 최대화하거나 타일로 배치한 창은 작업표시줄 위까지만 차지한다.
- 세션 홈에 `~/.config/tint2/tint2rc`가 있으면 창 관리자와 상관없이 그 파일을 쓴다. `vdesk shell`에서 위 기본 설정을 `~/.config/tint2/tint2rc`로 복사한 뒤 고친다. 설정 항목은 [tint2 매뉴얼](https://gitlab.com/o9000/tint2/-/blob/master/doc/tint2.md).
- 설정을 바꾼 뒤에는 `vdesk shell`에서 `pkill -x tint2; sleep 1; setsid vdesk-panel >/dev/null 2>&1 &`로 다시 띄운다.

## 이미지 갱신

- 이미지를 다시 빌드해도 실행 중인 세션은 이전 이미지를 계속 쓴다. `vdesk recreate TARGET` 또는 `vdesk recreate -a`로 옮긴다.
- `recreate`는 컨테이너만 교체하고 홈 volume, 이름, 시작 명령, 화면 번호, 포트는 유지한다. 세션 안의 앱은 다시 시작된다.
- Edge 등 패키지 업데이트: `vdesk build --no-cache` 후 `vdesk recreate -a`. `--pull`은 core의 base 이미지에만 적용된다.
- 이미지 이름은 `VDESK_IMAGE`(기본 `vdesk:latest`)와 `VDESK_CORE_IMAGE`(기본 `vdesk-core:latest`)로 바꿀 수 있다. 개발 중인 worktree를 기존 이미지와 나란히 시험할 때 쓴다: `VDESK_IMAGE=vdesk:dev VDESK_CORE_IMAGE=vdesk-core:dev ./vdesk build`, 같은 변수로 `./vdesk new`.

## 뷰어 선택

`attach`는 클라이언트 X 서버의 XInput 버전을 `xinput --version`으로 확인해 뷰어를 고른다.

| X 서버            | XInput | 뷰어              | 창 크기                             |
| ----------------- | ------ | ----------------- | ----------------------------------- |
| VcXsrv, MobaXterm | 2.x    | `xtigervncviewer` | 뷰어 창 크기에 가상 화면이 따라간다 |
| Xming 6.9         | 1.x    | `xtightvncviewer` | 고정. `-g WxH`로 지정               |

`xtigervncviewer` 1.13은 XInput 2가 없으면 "X Input 2 (or newer) is not available"로 종료한다.

## 데이터 위치

| 데이터                 | 위치                                                  |
| ---------------------- | ----------------------------------------------------- |
| 세션 홈 (앱 설정 전체) | Docker volume `vdesk-<user>-home-<id>`                |
| 다운로드               | 호스트 `~/Downloads` (모든 세션이 공유)               |
| 세션 이름              | `~/.local/state/vdesk/<hostname>/<id>.name`           |
| addon 설정             | `~/.config/vdesk/addons.enabled`, `addons/<name>/`    |
| 메뉴 규칙              | `~/.config/vdesk/menu.conf`                           |
| `run --host` 인증/로그 | `$XDG_RUNTIME_DIR/vdesk-<uid>/<id>.xauth`, `<id>.log` |
| 호스트 중계 로그       | `$XDG_RUNTIME_DIR/vdesk-<uid>/hostd.log`              |

- 홈이 NFS라서 세션 홈은 로컬 디스크의 volume에 둔다. Chromium 계열 프로필의 SQLite 잠금 문제를 피하기 위해서다.
- 세션 이름은 호스트별 디렉터리에 둔다. NFS 홈을 공유하는 다른 호스트와 섞이지 않게 하기 위해서다.

## 보안 설계

- `--network host`: 뷰어가 SSH X11 포워딩 포트(`127.0.0.1:60xx`)에 닿고, `run --host` 앱이 세션의 X socket(abstract socket)에 닿기 위해 쓴다.
- 화면 번호는 100부터, VNC 포트는 15900부터 비어 있는 것을 고른다. VNC는 `127.0.0.1`에만 열리고, 비밀번호는 세션이 시작할 때마다 새로 만들어 컨테이너 밖으로 꺼내지 않는다.
- X 화면은 host network namespace에 노출되므로 MIT-MAGIC-COOKIE 인증을 건다.
- Xvnc의 MIT-SHM 확장을 끈다. `run --host` 앱은 컨테이너와 IPC namespace가 달라 X 서버와 SysV 공유 메모리를 나눌 수 없고, Mesa(OpenGL) 앱은 이때 아무것도 그리지 못한다. 대신 모든 앱이 공유 메모리 없이 socket으로 화면을 보낸다. 호스트 IPC를 공유(`--ipc host`)하면 해결되지만 컨테이너가 호스트 사용자의 공유 메모리에 접근할 수 있게 되므로 쓰지 않는다.
- seccomp, `/dev/shm` 크기는 addon이 요구할 때만 바꾼다. 켜진 addon 중 하나라도 `SECCOMP=unconfined`면 모든 세션에 적용된다 (`edge`가 Chromium sandbox 때문에 요구).
