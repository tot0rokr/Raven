# vdesk 작업 지침

tmux처럼 쓰는 지속형 X 데스크톱 CLI. 사용법은 [README.md](./README.md), addon 작성은 [docs/addons.md](./docs/addons.md), 메뉴는 [docs/menu.md](./docs/menu.md).

## 파일

- `vdesk`: 호스트 CLI (POSIX sh + `local`). `docker run` 옵션, 세션 label 규칙, addon 조립 빌드가 모두 여기 있다. `~/.local/bin/vdesk`가 이 파일을 가리키는 symlink다.
- `Dockerfile`, `rootfs/`: core 이미지 (`vdesk-core:latest`). 앱을 넣지 않는다.
  - `rootfs/usr/local/bin/vdesk-entrypoint`: Xvnc, openbox-session, 시작 명령.
  - `rootfs/usr/local/bin/vdesk-menu`: openbox pipe menu 생성기 (규칙 + .desktop).
  - `rootfs/usr/local/bin/vdesk-apply-addon`: 빌드 중 addon 하나를 적용.
  - `rootfs/etc/vdesk/menu.d/00-core.conf`, `rootfs/etc/vdesk/Xresources`, `rootfs/etc/xdg/openbox/menu.xml`.
  - `rootfs/etc/xdg/openbox/autostart`: openbox가 뜰 때 tint2를 띄운다 (이미 떠 있으면 건너뜀).
  - `rootfs/etc/xdg/tint2/tint2rc`: 작업표시줄 기본 설정 (창 목록 + 시계). tint2는 `-c`로 실행해서 세션 홈에 복사본을 만들지 않게 한다.
- `addons/<name>/`: 기본 제공 addon. 지금은 `edge`만 있다.
- `docs/`: 사용자 매뉴얼.

## 규칙

- core와 `vdesk` CLI에 특정 앱 이름을 넣지 않는다. 앱별 설치, 설정, 런타임 요구사항은 addon에 둔다.
- 런타임 요구사항은 addon.conf → 이미지 label(`vdesk.seccomp`, `vdesk.shm-size`) → `run_container`로만 흐른다.
- 세션 메타데이터: 바뀌지 않는 값(id, display, port, cmd, geometry, user)은 container label, 바뀌는 값(이름)은 `~/.local/state/vdesk/<hostname>/`에 둔다.
- 세션 홈은 named volume에 둔다. 홈 디렉터리는 NFS다.
- 호스트에 패키지를 설치하지 않는다.
- `--no-sandbox`를 쓰지 않는다.
- Xvnc의 MIT-SHM은 끈 채로 둔다. 켜면 `run --host`로 띄운 Mesa 앱(wezterm 등)이 검은 창이 된다. `--ipc host`로 바꾸지 않는다.
- 구형 X 서버(Xming 6.9, XInput 1.x) 지원을 유지한다.
- CLI 명령이나 addon/메뉴 형식을 바꾸면 README와 `docs/`를 같이 고친다.

## 검증

- 사용자의 X 서버에 창을 띄우지 않는다. 화면 확인은 세션 화면을 직접 캡처하거나, 다른 세션 화면에 attach해서 본다.
- 사용자의 `~/.config/vdesk`와 사용자가 만든 세션을 건드리지 않는다. 시험할 때는 `XDG_CONFIG_HOME`을 임시 디렉터리로 돌리고, 이름 붙인 시험 세션을 만든 뒤 `kill`한다. `vdesk menu sync`와 `recreate -a`는 모든 세션에 영향을 준다.
- 순서:
  - `sh -n`으로 스크립트 문법 확인.
  - `vdesk build` → `vdesk addon ls`에서 BUILT 확인 → `docker image inspect`로 label 확인.
  - `vdesk new -d -s t xclock` → `vdesk menu show t` → `docker logs`에 `Zygote process exited`, `Fatal server error`가 없는지 확인 → `vdesk kill t`.
