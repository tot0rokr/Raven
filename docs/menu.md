# 메뉴 설정

메뉴는 열 때마다 `raven-menu`가 새로 만든다. 그래서 규칙을 고치거나 앱을 설치하면 다음에 메뉴를 열 때 바로 반영된다. 여는 방법은 창 관리자마다 다르고, 항목은 같다.

- xmonad 세션: `Alt+p` 또는 작업표시줄의 `menu` 버튼. rofi 목록에서 입력해 거르고 고른다 (`raven-launcher`).
- openbox 세션: 바탕화면 우클릭 (openbox pipe menu).

## 메뉴 구성

```
[규칙 파일의 항목, 파일 순서대로]
──────────
[.desktop 앱, 이름순]
```

- 규칙 파일: 아래 순서대로 읽고, 같은 라벨은 나중 규칙이 이긴다.
  1. `/etc/raven/menu.d/*.conf` (이미지 기본값. core는 `00-core.conf`, addon은 `50-<name>.conf`)
  2. `/etc/raven/menu.d/<창 관리자>/*.conf` (그 창 관리자 세션에서만 읽는다. 예: `menu.d/xmonad/00-core.conf`)
  3. 세션 안의 `~/.config/raven/menu.conf` (호스트의 `~/.config/raven/menu.conf`를 복사한 것)
- .desktop 앱: `/usr/share/applications`, `/usr/local/share/applications`, 세션 홈의 `~/.local/share/applications` 순서로 읽는다. 같은 파일 이름이면 뒤쪽이 이긴다.
  - `Type=Application`이 아니거나 `NoDisplay=true`, `Hidden=true`인 항목은 뺀다.
  - `Exec`의 `%U` 같은 field code는 지운다. `Terminal=true`면 `xterm -e`로 실행한다.

## 규칙 문법

| 형식               | 의미                                                                     |
| ------------------ | ------------------------------------------------------------------------ |
| `LABEL = COMMAND`  | 위쪽 영역에 항목 추가. 같은 LABEL의 .desktop 항목은 이 항목으로 대체된다 |
| `LABEL = @COMMAND` | 위와 같지만 COMMAND를 호스트에서 실행한다                                |
| `!LABEL`           | LABEL 항목을 숨긴다. 규칙 항목과 .desktop 항목 모두에 적용된다           |
| `---`              | 구분선                                                                   |
| `# ...`, 빈 줄     | 무시                                                                     |

- LABEL은 첫 `=` 앞부분이다. 앞뒤 공백은 지운다. LABEL에 `=`를 쓸 수 없다.
- COMMAND는 openbox가 shell 없이 인자로 나눠 실행한다. 파이프, `&&`, 환경 변수 확장이 필요하면 `sh -c '...'`로 감싼다.
- 같은 LABEL을 다시 정의하면 위치는 처음 정의한 자리를 유지하고 COMMAND만 바뀐다.

## Terminal 항목

라벨 `Terminal`은 xmonad 세션의 `Alt+Shift+Enter`가 실행하는 항목이다. core 기본 규칙은 `Terminal = xterm`이고, `menu.conf`에서 같은 라벨로 덮어쓰면 단축키와 메뉴가 함께 바뀐다. `!Terminal`로 숨기면 단축키는 xterm을 띄우지 못하고 오류 창을 띄운다.

```
Terminal = @wezterm --config 'front_end="WebGpu"' start --always-new-process
```

## 호스트 항목 (`@COMMAND`)

```
term = @wezterm --config 'front_end="WebGpu"' start --always-new-process
```

- 메뉴에서 누르면 세션이 호스트 중계(`raven-hostd`)에 라벨(`term`)만 보낸다. 중계는 호스트의 `~/.config/raven/menu.conf`에서 그 라벨의 마지막 규칙을 찾아, `@`로 시작하면 호스트에서 실행한다. 세션이 보낸 명령을 실행하는 일은 없다.
- 그래서 `@` 항목은 사용자의 `menu.conf`에서만 동작한다. 이미지 기본 규칙이나 addon의 `menu.conf`에 넣으면 메뉴에는 나오지만 누르면 오류 창이 뜬다.
- 명령줄에서는 `raven run -t TARGET @LABEL`로 같은 항목을 실행한다.
- 라벨이 곧 호스트 항목의 이름이다. 라벨을 바꾸면 `raven run @라벨`도 함께 바뀐다.
- 중계 프로세스와 문제 해결은 [usage.md](./usage.md#호스트-앱).

## 호스트에서 편집

```bash
raven menu edit          # $VISUAL / $EDITOR로 ~/.config/raven/menu.conf 편집 → 실행 중인 모든 세션에 복사
raven menu sync          # 편집기를 거치지 않고 고친 경우 수동 반영
raven menu show -t 0     # 세션 0에서 실제로 만들어지는 메뉴를 "LABEL<TAB>COMMAND"로 출력
```

- 편집기는 `$VISUAL`, `$EDITOR`, `vi` 순서로 고른다. vim이나 nvim이면 `--clean`으로 열어 개인 vimrc와 플러그인을 읽지 않는다.
- 세션을 attach, run, shell, recreate할 때도 자동으로 복사한다.
- 호스트 파일을 지우면 다음 sync 때 세션의 복사본도 지워진다.

## 예시

```
# ~/.config/raven/menu.conf

# 프로필별 Edge 항목을 위에 두고 기본 Edge 항목은 숨긴다
Edge (work) = microsoft-edge --profile-directory=Work
Edge (personal) = microsoft-edge --profile-directory=Personal
!Microsoft Edge
---
Clock = xclock -update 1
Logs = xterm -e sh -c 'tail -F /home/raven/Downloads/app.log'

# core 기본값인 Terminal 항목의 명령을 바꾼다
Terminal = xterm -fs 13
```

결과 (`raven menu show`):

```
Terminal          xterm -fs 13
Edge (work)       microsoft-edge --profile-directory=Work
Edge (personal)   microsoft-edge --profile-directory=Personal
---
Clock             xclock -update 1
Logs              xterm -e sh -c 'tail -F /home/raven/Downloads/app.log'
```

숨기지 않은 .desktop 앱이 있으면 마지막에 구분선과 함께 이어서 나온다. edge만 켠 이미지에서는 `Microsoft Edge`를 숨기면 남는 .desktop 앱이 없다.

## 세션별 항목

한 세션에만 항목을 두려면 그 세션 홈에 `.desktop` 파일을 만든다.

```bash
raven shell -t 0
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/sim.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Simulator
Exec=xterm -e /home/raven/Downloads/run-sim.sh
EOF
```

같은 방법으로 이미지의 `.desktop`과 같은 파일 이름에 `NoDisplay=true`를 넣으면 그 세션에서만 항목을 숨긴다.

## 기본값

- core (`00-core.conf`): `Terminal = xterm`을 넣고, 중복되는 `XTerm`, `UXTerm`, 세션 안에서 쓸 일이 없는 `TigerVNC Viewer`, 이미 떠 있는 작업표시줄을 하나 더 띄우는 `Tint2`를 숨긴다. 작업표시줄 설정 도구인 `Tint2 Settings`는 남긴다.
- core, xmonad 세션 (`xmonad/00-core.conf`): `Keyboard Shortcuts = raven-keys`. xmonad 단축키 목록을 rofi로 보여 준다 (`Alt+Shift+/`와 같음).
- addon이 기본 규칙을 넣으려면 addon 디렉터리에 `menu.conf`를 둔다 ([addons.md](./addons.md)).
