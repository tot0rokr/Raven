# vdesk 작업 지침

tmux처럼 쓰는 지속형 X 데스크톱 CLI. 개요와 설치는 [README.md](./README.md), 사용법은 [docs/usage.md](./docs/usage.md), addon 작성은 [docs/addons.md](./docs/addons.md), 메뉴는 [docs/menu.md](./docs/menu.md).

## 파일

- `vdesk`: 호스트 CLI (POSIX sh + `local`). `docker run` 옵션, 세션 label 규칙, addon 조립 빌드가 모두 여기 있다. `~/.local/bin/vdesk`가 이 파일을 가리키는 symlink다.
- `Dockerfile`, `rootfs/`: core 이미지 (`vdesk-core:latest`). 앱을 넣지 않는다.
  - 첫 단계(`xmonad-build`)에서 `xmonad/xmonad.hs`를 GHC로 컴파일해 `xmonad-x86_64-linux`만 최종 이미지에 복사한다.
  - `rootfs/usr/local/bin/vdesk-entrypoint`: Xvnc, D-Bus 세션 버스, 창 관리자(`VDESK_WM`: xmonad 또는 openbox), 시작 명령. 버스 주소는 `Dockerfile`의 `ENV DBUS_SESSION_BUS_ADDRESS`로 고정해서 `docker exec` 셸도 같은 버스를 쓴다.
  - `rootfs/usr/local/bin/vdesk-menu`: 메뉴 생성기 (규칙 + .desktop). openbox pipe menu XML, `--list`는 탭 구분 목록.
  - `rootfs/usr/local/bin/vdesk-launcher`: xmonad용 메뉴. `vdesk-menu --list`를 rofi로 고른다.
  - `rootfs/usr/local/bin/vdesk-keys`: `/etc/vdesk/keys/$VDESK_WM`(키<TAB>설명)을 rofi 목록으로 보여 준다. xmonad의 `Alt+Shift+/`와 메뉴의 `Keyboard Shortcuts`가 부른다.
  - `rootfs/usr/local/bin/vdesk-panel`: tint2를 한 번만 띄운다. 창 관리자별 기본 설정에 addon 버튼(`/etc/vdesk/panel.d/*.tint2rc`)을 붙여 `/tmp/vdesk-tint2rc`로 띄운다.
  - `rootfs/usr/local/bin/vdesk-wallpaper`: 세션 홈의 `wallpaper.local.*`, 없거나 쓸 수 없으면 `wallpaper.*`로 배경을 칠한다. 이미지는 `xwallpaper --daemon`(크기 변경 시 다시 맞춤), 단색은 `xsetroot`.
  - `rootfs/usr/local/bin/vdesk-apply-addon`: 빌드 중 addon 하나를 적용.
  - `rootfs/usr/local/bin/vdesk-session-hooks`: addon이 둔 `/etc/vdesk/session.d/*`를 `start`(entrypoint, 창 관리자보다 먼저)와 `reload`(vdesk가 홈 파일을 복사한 뒤)에 실행.
  - `rootfs/etc/vdesk/menu.d/00-core.conf`, `rootfs/etc/vdesk/menu.d/xmonad/00-core.conf`(창 관리자별 규칙), `rootfs/etc/vdesk/Xresources`, `rootfs/etc/xdg/openbox/menu.xml`.
  - `rootfs/etc/xdg/openbox/autostart`: openbox가 뜰 때 `vdesk-panel`을 부른다.
  - `rootfs/etc/xdg/tint2/tint2rc` (openbox), `rootfs/etc/vdesk/tint2/xmonad.tint2rc` (xmonad): 작업표시줄 기본 설정. tint2는 `-c`로 실행해서 세션 홈에 복사본을 만들지 않게 한다.
- `xmonad/xmonad.hs`: xmonad 설정. mod는 Alt(Windows가 Win 키를 가로챔). EWMH와 docks로 tint2와 연동한다. `XMonad.Layout.Spacing`으로 창 사이와 화면 가장자리에 6px 간격(각 3px)을 두고 `Alt+g`로 끈다.
- `vdesk-hostd`: 호스트 중계 (python3). abstract socket `@vdesk-host-<uid>`로 세션의 요청(세션 id + 라벨)을 받아 `vdesk run -t ID @LABEL`로 넘긴다. 시작한 `vdesk`의 경로와 환경 변수를 물려받는다.
  - `rootfs/usr/local/bin/vdesk-host`: 세션 쪽 요청 프로그램 (python3). 실패하면 `xmessage`로 알린다.
- `addons/<name>/`: 기본 제공 addon. `edge`(기본으로 켜짐), `kime`(한글 입력기, upstream .deb를 버전과 sha256 고정. VcXsrv가 한/영 키를 왼쪽 Alt로 보내서 작업표시줄 `한/A` 버튼이 `xdotool`로 Hangul 키를 보낸다).
- `docs/`: 사용자 매뉴얼.

## 규칙

- core와 `vdesk` CLI에 특정 앱 이름을 넣지 않는다. 앱별 설치, 설정, 런타임 요구사항은 addon에 둔다.
- 런타임 요구사항은 addon.conf → 이미지 label(`vdesk.seccomp`, `vdesk.shm-size`) → `run_container`로만 흐른다. 세션 환경 변수는 addon.conf `SESSION_ENV` → 이미지 `ENV`로만 넣는다.
- 홈 overlay(`~/.config/vdesk/home`)는 세션 사용자 권한의 `tar --no-overwrite-dir`로 푼다. 이미 있는 디렉터리의 권한과 시각을 바꾸지 않기 위해서다. `docker cp`는 묶음의 디렉터리 항목으로 기존 디렉터리의 권한과 시각을 덮어써서 쓰지 않는다 (세션의 `~/.config`가 775로 바뀐 적이 있다). 새 세션과 멈춘 세션은 시작 전에 볼륨으로 풀어서 세션 hook이 처음부터 그 파일을 보게 한다.
- 세션 메타데이터: 바뀌지 않는 값(id, display, port, cmd, geometry, user)은 container label, 바뀌는 값(이름)은 `~/.local/state/vdesk/<hostname>/`에 둔다.
- 세션 홈은 named volume에 둔다. 홈 디렉터리는 NFS다.
- 호스트에 패키지를 설치하지 않는다.
- `--no-sandbox`를 쓰지 않는다.
- Xvnc의 MIT-SHM은 끈 채로 둔다. 켜면 `run --host`로 띄운 Mesa 앱(wezterm 등)이 검은 창이 된다. `--ipc host`로 바꾸지 않는다.
- 세션과 호스트 사이에는 메뉴 라벨만 오간다. 호스트 명령은 호스트의 `~/.config/vdesk/menu.conf`에 있는 `LABEL = @COMMAND` 규칙에서만 찾고, 세션이 보낸 문자열을 명령으로 실행하지 않는다. 중계는 SO_PEERCRED로 같은 uid만 받는다.
- 구형 X 서버(Xming 6.9, XInput 1.x) 지원을 유지한다.
- GHC는 빌드 단계에만 둔다. xmonad 실행 파일 이름은 `xmonad-x86_64-linux`로 둔다. 다른 이름이면 xmonad가 시작할 때마다 GHC로 다시 컴파일하려 한다. 재시작 키도 `restart "xmonad-x86_64-linux"`로 GHC 없이 재시작한다.
- 창 관리자 label(`vdesk.wm`)이 없는 세션은 openbox로 다룬다.
- 배경화면 설정은 이미지 label `vdesk.features`에 `wallpaper`가 있는 세션에만 복사한다(core `Dockerfile`이 붙이고 addon 이미지가 물려받는다). 이전 이미지 세션의 홈에는 파일을 쓰지 않는다.
- 배경화면 파일은 conf를 먼저 지우고, 이미지와 conf를 임시 파일을 거쳐 `mv`로 바꾼다. 중간에 끊겨도 conf와 다른 이미지가 짝지어지지 않고, 다음 복사 때 다시 한다. 세션 사본은 이미지 sha가 conf의 `sha=`와 다르면(동시에 쓴 경우) 다시 복사한다. 호스트 쪽에 잠금을 두지 않는 이유는 홈이 NFS여서 잠금에서 멈출 수 있기 때문이다.
- xwallpaper는 `--daemon --no-atoms`로 띄운다. Ubuntu 24.04의 0.7.3은 root pixmap atom을 갱신하다가, 이전 배경 pixmap이 없던 세션(새로 만들거나 재시작한 세션)에서 첫 크기 변경 때 자기 X 연결을 kill하고 죽는다(upstream 커밋 "Kill clients only once"에서 고침). 그러면 배경이 그때 크기에 멈춘다.
- `attach -g`는 모드를 만들어 출력에 건다. `xrandr --fb`로 바꾸지 않는다(아래 검증 항목과 같은 이유).
- CLI 명령이나 addon/메뉴 형식을 바꾸면 `docs/usage.md`와 관련 `docs/`를 같이 고친다. README는 기능 목록과 구조가 바뀔 때 고친다.
- xmonad 키를 바꾸면 `rootfs/etc/vdesk/keys/xmonad`(세션 안 목록)와 `docs/usage.md`의 키 표를 같이 고친다.

## 검증

- 사용자의 X 서버에 창을 띄우지 않는다. 화면 확인은 세션 화면을 직접 캡처하거나, 다른 세션 화면에 attach해서 본다. 시험 셸에서는 먼저 `unset DISPLAY`를 하고, 인자 없는 `vdesk`(새 세션 + attach)를 실행하거나 `source`하지 않는다.
- 중계(`vdesk-hostd`)는 처음 띄운 `vdesk`의 경로와 `XDG_CONFIG_HOME`을 계속 쓴다. 시험용 설정이나 worktree로 시험한 뒤에는 `vdesk hostd stop`으로 내려 둔다.
- worktree에서 빌드할 때는 `VDESK_IMAGE`, `VDESK_CORE_IMAGE`로 다른 태그를 써서 main이 쓰는 `vdesk:latest`를 덮어쓰지 않는다.
- 화면 크기 변경은 `xrandr --fb`로 줄이지 않는다. 출력보다 작게 주면 실패하면서 VNC-0 출력이 꺼지고, 그 세션은 배경 등 출력 기반 동작이 깨진다. 실제와 같은 크기 변경은 다른 시험 세션 화면에 뷰어를 attach해서(RemoteResize) 만든다.
- 사용자의 `~/.config/vdesk`와 사용자가 만든 세션을 건드리지 않는다. 시험할 때는 `XDG_CONFIG_HOME`을 임시 디렉터리로 돌리고, 이름 붙인 시험 세션을 만든 뒤 `kill`한다. `vdesk menu sync`, `recreate -a`, `-t` 없는 `wallpaper set/color/clear`와 `home sync`는 모든 세션에 영향을 준다. 시험에서는 `-t 시험세션`만 쓴다. 시험용 설정으로 사용자 세션에 `attach`, `run`을 해도 그 설정의 메뉴, 배경, 홈 파일이 사용자 세션에 복사된다.
- 뷰어를 `timeout`으로 끝내도 세션 안의 `xtigervncviewer`는 남는다(`docker exec` 클라이언트만 끝남). 남은 뷰어는 RemoteResize로 화면 크기를 되돌리므로 크기 시험 전에 `docker exec <컨테이너> pkill -x xtigervncviewer`로 내린다.
- 순서:
  - `sh -n`으로 스크립트 문법 확인.
  - `vdesk build` → `vdesk addon ls`에서 BUILT 확인 → `docker image inspect`로 label 확인.
  - `vdesk new -d -s t xclock` → `vdesk menu show t` → `docker logs`에 `Zygote process exited`, `Fatal server error`가 없는지 확인 → `vdesk kill t`.
