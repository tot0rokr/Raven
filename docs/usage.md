# vdesk 사용법

설치와 개요는 [README](../README.md), 메뉴 규칙은 [menu.md](./menu.md), addon 작성은 [addons.md](./addons.md).

## 접속 준비

- 클라이언트에 X 서버를 띄운다. Windows는 VcXsrv를 권장한다. Xming 6.9도 되지만 뷰어 창 크기가 고정된다 ([뷰어](#뷰어)).
- X11 forwarding을 켜고 VM에 접속한다.
  - OpenSSH: `ssh -Y user@vm`. Windows OpenSSH는 먼저 `DISPLAY`를 정한다 (cmd: `set DISPLAY=127.0.0.1:0.0`, PowerShell: `$env:DISPLAY="127.0.0.1:0.0"`).
  - PuTTY: Connection > SSH > X11 > Enable X11 forwarding.
- 접속한 셸에서 `echo $DISPLAY`가 `localhost:10.0` 같은 값이면 된다. 비어 있으면 `new`와 `attach`가 `DISPLAY is not set`으로 멈춘다.
- tmux 안의 기존 셸은 이전 접속의 `DISPLAY`를 갖고 있다. 다시 접속한 뒤에는 새 창을 열거나 `eval "$(tmux show-environment -s DISPLAY)"`로 갱신한다.

## 명령 요약

| 명령                      | 하는 일                                                   |
| ------------------------- | --------------------------------------------------------- |
| `vdesk`, `vdesk new`      | 세션을 만들고 attach                                      |
| `vdesk attach`, `vdesk a` | 세션에 attach                                             |
| `vdesk ls`                | 세션 목록                                                 |
| `vdesk rename`            | 세션 이름 변경                                            |
| `vdesk run`               | 세션 화면에 앱 실행 (세션 안 또는 호스트)                 |
| `vdesk shell`             | 세션 안의 bash                                            |
| `vdesk kill`              | 세션과 홈 삭제                                            |
| `vdesk recreate`          | 세션을 현재 이미지로 교체 (홈 유지)                       |
| `vdesk build`             | core 이미지와 켜진 addon으로 이미지 빌드                  |
| `vdesk addon`             | addon 목록, 켜기, 끄기, 만들기                            |
| `vdesk menu`              | 메뉴 규칙 편집, 반영, 확인                                |
| `vdesk home`              | 개인 설정 파일(`~/.config/vdesk/home`)을 세션 홈에 복사   |
| `vdesk hostd`             | 호스트 중계 상태, 시작, 중지                              |
| `vdesk wallpaper`         | 배경화면 지정, 해제, 확인                                 |
| `vdesk help`              | 사용법 출력                                               |

## 세션

### 만들기

```bash
vdesk                                  # 새 세션(xmonad, xterm) 생성 후 attach
vdesk new -s web microsoft-edge        # 이름과 시작 명령 지정
vdesk new -w openbox -s ob             # 창 관리자를 openbox로
vdesk new -d -s sim xclock             # 만들기만 하고 attach하지 않음
vdesk -d -s sim xclock                 # 옵션으로 시작하면 new로 본다
```

| 옵션      | 의미                                              | 기본값                   |
| --------- | ------------------------------------------------- | ------------------------ |
| `-s NAME` | 세션 이름                                         | 없음                     |
| `-d`      | 만들기만 하고 attach하지 않는다                   | attach                   |
| `-g WxH`  | 가상 화면 크기                                    | `1600x900`               |
| `-w WM`   | 창 관리자 `xmonad` 또는 `openbox`                 | `xmonad` (`VDESK_WM`)    |
| `CMD...`  | 시작 명령. 세션이 다시 뜰 때마다 다시 실행된다    | `xterm`                  |

- id는 0부터 비어 있는 가장 작은 번호다. 홈 volume이 남아 있는 번호도 건너뛴다.
- 이름은 영문자로 시작하고 영문자, 숫자, `_`, `.`, `-`만 쓴다. 세션끼리 겹칠 수 없다.
- 화면 번호는 `:100`부터, VNC 포트는 15900부터 비어 있는 것을 쓴다.

### TARGET

- 세션 id 또는 이름이다. 생략하면 가장 최근에 만들거나 `recreate`한 세션을 쓴다. `kill`과 `recreate`는 생략할 수 없다.
- `attach`, `shell`, `kill`, `recreate`, `rename`, `menu show`는 마지막 인자나 `-t TARGET`으로 받는다. `run`과 `wallpaper`는 `-t TARGET`만 받는다.
- 옵션은 TARGET 앞에 둔다 (`vdesk a -g 1920x1000 0`).

### attach와 detach

```bash
vdesk a web                            # 이름으로 attach
vdesk a -g 1920x1000 0                 # id로 attach, 가상 화면 크기 변경
vdesk a -d 0                           # 다른 뷰어를 끊고 attach
```

- 뷰어 창을 닫으면 detach다. 세션 안의 앱은 계속 실행된다.
- 한 세션에 뷰어 여러 개가 동시에 붙을 수 있다. `-d`는 나머지 뷰어를 끊는다.
- 멈춘 세션은 `attach`, `run`, `shell` 때 자동으로 켠다.

### 목록과 이름

```bash
vdesk ls
vdesk rename 0 browser
```

```
ID   NAME             STATE    WM       DISPLAY  PORT   VIEWERS  CMD
0    browser          running  xmonad   :100     15900  1        microsoft-edge
1    ob               exited   openbox  :101     15901  -        xterm
```

- `VIEWERS`는 지금 붙어 있는 뷰어 수다. 멈춘 세션은 `-`.
- 창 관리자를 고를 수 있기 전에 만든 세션은 `WM`이 openbox다.

### 앱 실행과 셸

```bash
vdesk run -t web xclock                # 세션 안(컨테이너)에서 실행
vdesk run -t web 'xterm -e top'        # sh -c로 실행하므로 셸 문법을 쓸 수 있다
vdesk run -t 0 --host gtkwave a.vcd    # 호스트 앱을 세션 화면에 실행
vdesk run -t 0 @Terminal               # menu.conf의 호스트 항목을 라벨로 실행
vdesk shell -t 0                       # 세션 안의 bash (DISPLAY 설정됨)
```

- `run`은 앱을 띄우고 바로 돌아온다.
- `--host`(`-H`)와 `@LABEL`은 [호스트 앱](#호스트-앱).

### 재시작과 보존

세션 컨테이너는 `--restart unless-stopped`로 실행된다. VM이 재부팅되면 다시 뜨고, 시작 명령(`new`의 CMD)도 다시 실행된다. 열려 있던 다른 창은 다시 뜨지 않는다.

| 데이터                                  | 재시작 (재부팅) | `recreate` | `kill` |
| --------------------------------------- | --------------- | ---------- | ------ |
| 세션 홈 `/home/vdesk` (앱 설정, 프로필) | 유지            | 유지       | 삭제   |
| 홈 밖의 파일 (컨테이너 파일시스템)      | 유지            | 삭제       | 삭제   |
| `~/Downloads` (호스트 디렉터리)         | 유지            | 유지       | 유지   |
| 세션 이름, 시작 명령, 화면 번호, 포트   | 유지            | 유지       | 삭제   |

재부팅 뒤 추가로 띄울 앱은 세션 홈에 적는다 (`vdesk shell`로 편집). 둘 다 `app &` 형식의 셸 스크립트다.

- xmonad 세션: `~/.config/vdesk/autostart`
- openbox 세션: `~/.config/openbox/autostart`

### 지우기

```bash
vdesk kill sim
```

컨테이너, 홈 volume, 세션 이름, 호스트 앱 로그를 지운다. 되돌릴 수 없다. `~/Downloads`는 호스트 디렉터리라 남는다.

## 창 관리자

세션마다 `vdesk new -w xmonad|openbox`로 고른다. 기본은 xmonad이고 `VDESK_WM` 환경 변수로 바꾼다. `recreate`해도 유지된다.

### xmonad

타일링 창 관리자다. 새 창이 화면을 나눠 갖고, 대화상자와 오류 창은 가운데에 떠 있는 창으로 뜬다. 설정(`xmonad/xmonad.hs`)은 이미지 빌드 단계에서 컴파일되어 들어가므로, 바꾸려면 파일을 고치고 `vdesk build` 후 새 세션을 만들거나 `recreate`한다.

Windows가 Win 키를 가로채므로 조작 키(mod)는 Alt다.

| 키                                      | 동작                                              |
| --------------------------------------- | ------------------------------------------------- |
| `Alt+p`                                 | 메뉴 (rofi, 작업표시줄의 `menu` 버튼과 같음)      |
| `Alt+Shift+Enter`                       | 메뉴의 `Terminal` 항목 (기본 xterm)               |
| `Alt+j` / `Alt+k`                       | 다음 / 이전 창으로 포커스                         |
| `Alt+Shift+j` / `Alt+Shift+k`           | 포커스된 창을 다음 / 이전 자리와 바꿈             |
| `Alt+Enter`                             | 포커스된 창을 주 영역으로                         |
| `Alt+h` / `Alt+l`                       | 주 영역 줄이기 / 늘리기                           |
| `Alt+,` / `Alt+.`                       | 주 영역 창 수 늘리기 / 줄이기                     |
| `Alt+\`, `Alt+Space`                    | 레이아웃 전환 (좌우 분할, 상하 분할, 전체 화면)   |
| `Alt+1`..`9`                            | 작업 공간 이동                                    |
| `Alt+Shift+1`..`9`                      | 포커스된 창을 작업 공간으로 보냄                  |
| `Alt+t`                                 | 떠 있는 창을 다시 타일로                          |
| `Alt+왼쪽 드래그` / `Alt+오른쪽 드래그` | 창을 띄워서 이동 / 크기 조절                      |
| `Alt+Shift+c`                           | 포커스된 창 닫기                                  |
| `Alt+g`                                 | 창 사이 간격 끄기 / 켜기                          |
| `Alt+q`                                 | xmonad 재시작 (창은 유지)                         |

- `Alt+Tab`은 Windows가 가로챈다. 창 이동은 `Alt+j`/`Alt+k`를 쓴다. `Alt+Space`도 Windows가 가로챌 수 있어서 레이아웃 전환은 `Alt+\`를 쓴다.
- `Alt+Shift+Enter`가 띄우는 터미널은 `menu.conf`의 `Terminal` 항목으로 바꾼다. 예: `Terminal = @wezterm start --always-new-process` (호스트 wezterm). 메뉴의 Terminal도 같이 바뀐다.
- 창 사이와 화면 가장자리에 6px 간격을 둔다. 창이 하나여도 둔다. 배경화면은 이 간격과 빈 작업 공간에서 보인다. `Alt+g`로 끄면 창이 화면을 빈틈없이 채운다. 세션이 다시 뜨면 켜진 상태로 돌아간다.

### openbox

바탕화면 우클릭 메뉴와 창 제목 표시줄이 있는 일반 데스크톱이다. 작업표시줄 버튼으로 최소화와 복원을 한다.

## 메뉴

xmonad 세션은 `Alt+p`나 작업표시줄의 `menu` 버튼, openbox 세션은 바탕화면 우클릭으로 연다. 메뉴는 열 때마다 규칙 파일과 설치된 앱의 `.desktop` 파일로 새로 만들어진다.

```bash
vdesk menu edit                        # ~/.config/vdesk/menu.conf 편집 후 실행 중인 모든 세션에 반영
vdesk menu sync                        # 편집기 없이 고친 경우 수동 반영
vdesk menu show -t 0                   # 세션 0의 실제 메뉴를 "LABEL<TAB>COMMAND"로 출력
```

```
# ~/.config/vdesk/menu.conf
Edge (work) = microsoft-edge --profile-directory=Work
Terminal = @wezterm start --always-new-process
!Microsoft Edge
```

- 편집기는 `$VISUAL`, `$EDITOR`, `vi` 순서로 고른다. vim이나 nvim이면 `--clean`으로 연다.
- 세션을 다룰 때(`new`, `attach`, `run`, `shell`, `recreate`)도 호스트의 `menu.conf`를 세션에 복사한다.
- 규칙 문법, 우선순위, 세션별 항목은 [menu.md](./menu.md).

## 작업표시줄

세션 화면 아래쪽에 [tint2](https://gitlab.com/o9000/tint2) 작업표시줄이 있다. 오른쪽은 날짜와 시각이다.

- xmonad 세션: 왼쪽부터 `menu` 버튼, 작업 공간별 창 목록이다. 현재 작업 공간이 강조된다. 창 버튼을 누르면 그 창으로 이동하고, 가운데 버튼은 창을 닫는다. 기본 설정은 이미지의 `/etc/vdesk/tint2/xmonad.tint2rc`.
- openbox 세션: 열린 창 목록이고, 창 버튼을 누르면 최소화와 복원이 바뀐다. 기본 설정은 이미지의 `/etc/xdg/tint2/tint2rc`.
- 최대화하거나 타일로 배치한 창은 작업표시줄 위까지만 차지한다.
- 세션 홈에 `~/.config/tint2/tint2rc`가 있으면 창 관리자와 상관없이 그 파일을 쓴다. `vdesk shell`에서 위 기본 설정을 복사한 뒤 고친다. 설정 항목은 [tint2 매뉴얼](https://gitlab.com/o9000/tint2/-/blob/master/doc/tint2.md).
- 설정을 바꾼 뒤에는 `vdesk shell`에서 `pkill -x tint2; sleep 1; setsid vdesk-panel >/dev/null 2>&1 &`로 다시 띄운다.

## 호스트 앱

호스트에 설치된 GUI 앱을 세션 화면에 띄운다.

```bash
vdesk run -t web --host wezterm start --always-new-process   # 명령을 직접 지정
vdesk run -t web @Terminal                                    # menu.conf의 호스트 항목 라벨로
vdesk run -t web @"Host shell"                                # 라벨에 공백이 있을 때
```

- 호스트 프로세스라 격리되지 않는다. 호스트의 홈, 설정, 셸 환경을 그대로 쓴다.
- SSH가 끊겨도 세션 화면에 남는다. 세션을 `kill`하거나 `recreate`하면 화면이 사라지면서 함께 종료된다.
- 출력과 오류는 `$XDG_RUNTIME_DIR/vdesk-<uid>/<id>.log`에 쌓인다.

### 메뉴의 호스트 항목

`vdesk menu edit`으로 `menu.conf`를 열고 명령 앞에 `@`를 붙인 항목을 적는다.

```
Terminal = @wezterm start --always-new-process
```

- 세션 메뉴에서 누르면 호스트에서 실행된다. 명령줄에서는 `vdesk run -t TARGET @Terminal`.
- 메뉴 항목은 컨테이너 안에서 실행되므로 호스트 중계 `vdesk-hostd`에 요청을 보낸다. 세션은 라벨만 보내고, 명령은 호스트의 `~/.config/vdesk/menu.conf`에서만 찾는다. 같은 사용자의 요청만 받는다.
- 그래서 `@` 항목은 사용자의 `menu.conf`에서만 동작한다. 이미지 기본 규칙이나 addon의 `menu.conf`에 넣으면 메뉴에는 나오지만 누르면 오류 창이 뜬다.
- 호스트 항목 기능 이전 이미지로 만든 세션에서는 메뉴에서 실행되지 않는다. `vdesk recreate`로 옮긴다. 명령줄의 `vdesk run @LABEL`은 이미지와 상관없이 된다.

### 호스트 중계

```bash
vdesk hostd                            # 상태 (pid, 로그 위치)
vdesk hostd start
vdesk hostd stop
```

- 세션을 다루는 명령(`new`, `attach`, `run`, `shell`, `recreate`)이 실행될 때 중계가 없으면 띄운다. VM 재부팅 뒤 메뉴에서 호스트 앱이 안 뜨면 `vdesk hostd start`를 실행한다.
- 중계는 처음 띄운 `vdesk`의 경로와 환경 변수(`XDG_CONFIG_HOME` 등)를 계속 쓴다.
- 로그는 `$XDG_RUNTIME_DIR/vdesk-<uid>/hostd.log`.

### wezterm

- 호스트의 `~/.wezterm.lua`를 그대로 쓴다. GPU 없이 Mesa llvmpipe로 그리므로 호스트에 `libegl1`, `libegl-mesa0`이 있어야 한다.
- `--always-new-process`: 다른 화면에서 실행 중인 wezterm 프로세스에 새 창이 붙어 엉뚱한 화면에 뜨는 것을 막는다.

## 배경화면

```bash
vdesk wallpaper set ~/Pictures/bg.jpg               # 모든 세션 (채우기 방식 zoom)
vdesk wallpaper set -m maximize ~/Pictures/bg.png   # 채우기 방식 지정
vdesk wallpaper set -t web ~/Pictures/b2.jpg        # 이 세션만
vdesk wallpaper color -t web '#24303c'              # 단색 (세션 구분용)
vdesk wallpaper clear [-t TARGET]                   # 해제
vdesk wallpaper                                     # 현재 설정 보기
```

- 이미지는 PNG와 JPEG만 받는다.
- 채우기 방식(`-m`)은 xwallpaper의 것을 쓴다: `zoom`(꽉 채우고 넘치는 부분 자름), `maximize`(비율 유지, 전체가 보이게), `center`, `tile`, `stretch`, `focus`.
- `-t` 없이 지정하면 모든 세션에 적용된다. 호스트의 `~/.config/vdesk/wallpaper.conf`, `wallpaper.img`에 저장되고, 실행 중인 세션에는 바로, 멈춘 세션에는 다음에 다룰 때 복사된다.
- `-t`로 지정하면 그 세션 홈에만 저장되고(`wallpaper.local.*`) 전체 배경보다 우선한다. `clear -t`로 지우면 전체 배경으로 돌아간다. 세션 홈에 있으므로 `recreate`해도 유지된다. 멈춘 세션에 `-t`를 주면 그 세션을 켠다.
- `vdesk wallpaper`는 전체 설정과, 실행 중인 세션 중 자기 설정이 있는 세션만 보여 준다. 세션 설정의 형식이 잘못됐으면 `(invalid)`로 보이고 전체 배경이 대신 칠해진다.
- 세션에 복사하지 못하면 `wallpaper not updated` 경고만 내고 attach, run은 그대로 진행한다. 복사는 다음에 그 세션을 다룰 때 다시 한다.
- 뷰어 창 크기에 맞춰 화면 크기가 바뀌면 배경을 다시 맞춘다(`xwallpaper --daemon`).
- 배경화면 기능 이전 이미지로 만든 세션에는 복사하지 않는다. `vdesk recreate` 뒤부터 적용된다.

## 개인 설정 파일

호스트의 `~/.config/vdesk/home/`은 세션 홈(`/home/vdesk`)과 같은 구조다. 여기 둔 파일은 모든 세션 홈의 같은 경로로 복사된다. 설정은 사람마다 각자의 호스트 `~/.config/vdesk/`에 있으므로, 같은 VM을 쓰는 사람끼리 섞이지 않는다.

```bash
mkdir -p ~/.config/vdesk/home/.config
ln -s ~/dotfiles/.config/kime ~/.config/vdesk/home/.config/kime   # dotfiles의 설정을 연결
vdesk home sync                        # 실행 중인 모든 세션에 바로 반영
vdesk home sync -t web                 # 이 세션만
```

- 세션을 다룰 때(`new`, `attach`, `run`, `shell`, `recreate`) 바뀐 것이 있으면 복사한다. 새 세션과 멈춘 세션은 시작하기 전에 복사하므로, 세션 안의 데몬(kime 등)이 처음부터 이 설정을 쓴다.
- 실행 중인 세션에 복사하면 addon의 세션 hook이 바뀐 설정을 다시 읽는다 ([addons.md](./addons.md#세션-hook)).
- symlink는 호스트에서 따라가서, 가리키는 내용을 복사한다. dotfiles 저장소를 가리키게 두면 된다.
- 호스트가 기준이다. 여기 있는 경로는 세션 쪽 사본을 덮어쓴다. 여기 없는 세션 파일은 건드리지 않고, 이미 있는 디렉터리의 권한도 바꾸지 않는다. 여기서 파일을 지워도 세션의 사본은 남는다.
- 바뀌었는지는 묶음의 sha256으로 판단한다. 기록은 `~/.local/state/vdesk/<hostname>/<id>.home`. 작은 설정 파일용이다. 큰 파일을 두면 세션을 다룰 때마다 묶는 시간이 든다.
- 세션 하나만 다르게 하려면 `vdesk shell`로 그 세션 홈에서 고친다. 이 디렉터리에 있는 경로는 다음 복사 때 덮어써진다.

## 한글 입력

`kime` addon을 켜면 세션 안에서 kime로 한글을 입력한다. Windows IME가 조합한 글자는 VcXsrv를 거쳐 세션으로 넘어오지 않으므로, 한글 조합은 세션 안의 입력기가 한다.

```bash
vdesk addon enable kime
vdesk build
vdesk recreate web                     # 기존 세션은 recreate, 새 세션은 바로 적용
```

- 설정이 없으면 kime 기본값(두벌식, 한/영 키로 전환)이다. 내 설정은 `~/.config/vdesk/home/.config/kime/`에 둔다 ([개인 설정 파일](#개인-설정-파일)).
- GTK3 앱(Edge 포함)은 GTK 입력 모듈로, xterm 같은 X 앱은 XIM으로 kime에 붙는다. 환경 변수 `GTK_IM_MODULE`, `QT_IM_MODULE`, `XMODIFIERS`는 addon이 이미지에 넣는다.
- kime 설정이 바뀌어 복사되면 kime가 다시 뜬다. 이미 열린 xterm 같은 XIM 앱은 다시 열어야 한글이 된다.
- 한/영 키가 X에 어떤 키로 오는지는 Windows 키보드 드라이버에 따라 다르다(`Hangul` 또는 `Alt_R`). 전환이 안 되면 `vdesk shell`에서 `xev -event keyboard | grep keysym`을 실행하고, 세션 화면에 뜬 창에서 그 키를 눌러 본다. 나온 이름을 kime 설정의 `global_hotkeys`에 넣는다.
- Windows 쪽 IME는 영문 상태로 둔다. 한글 상태면 Windows IME가 키를 먼저 가로챌 수 있다.
- kime의 상태 표시(indicator)와 Wayland 모듈은 세션에서 뜨지 않는다. 설정에 있어도 XIM과 GTK 입력에는 영향이 없다.
- 로그는 세션 안의 `/tmp/vdesk-kime.log`.

## 파일 주고받기

- 세션의 `~/Downloads`는 호스트 `~/Downloads`를 그대로 연결한 것이다. 모든 세션이 같은 디렉터리를 쓴다. Edge로 받은 파일도 여기 생긴다.
- 그 밖의 세션 파일은 `docker cp`로 꺼낸다. 컨테이너 이름은 `vdesk-<user>-<id>`다.

```bash
docker cp vdesk-$USER-0:/home/vdesk/.config/vdesk/autostart .
```

## 앱 추가 (addon)

```bash
vdesk addon ls                         # NAME, ENABLED, BUILT(현재 이미지에 포함), SOURCE, DESCRIPTION
vdesk addon new gtkwave gtkwave        # apt 패키지만 쓰는 addon 생성
vdesk addon enable gtkwave             # 켜기 (disable로 끄기)
vdesk build                            # 이미지 재빌드
vdesk recreate -a                      # 모든 세션을 새 이미지로 교체 (홈 유지)
```

- 기본 제공 addon은 `~/vdesk/addons/`, 사용자 addon은 `~/.config/vdesk/addons/`에 둔다. 이름이 같으면 사용자 addon이 우선한다.
- 켜진 목록은 `~/.config/vdesk/addons.enabled`다. 파일이 없으면 `edge`만 켜진 것으로 본다.
- 저장소 등록, 설정 파일, 실행 옵션 고정 등 작성법은 [addons.md](./addons.md).

## 이미지 갱신

```bash
vdesk build                            # core + 켜진 addon으로 vdesk:latest 빌드
vdesk build --no-cache                 # Edge 등 패키지까지 새로 받기
vdesk recreate web                     # 세션 하나를 새 이미지로
vdesk recreate -a                      # 모든 세션을 새 이미지로
```

- 이미지를 다시 빌드해도 실행 중인 세션은 이전 이미지를 계속 쓴다. `recreate`로 옮긴다.
- `recreate`는 컨테이너만 교체하고 홈 volume, 이름, 시작 명령, 화면 번호, 포트, 창 관리자는 유지한다. 세션 안의 앱은 다시 시작된다.
- `build`의 인자는 `docker build`에 그대로 넘긴다. `--pull`은 core의 base 이미지에만 적용된다.

## 뷰어

`attach`는 클라이언트 X 서버의 XInput 버전을 확인해 뷰어를 고른다.

| X 서버    | XInput | 뷰어              | 창 크기                             |
| --------- | ------ | ----------------- | ----------------------------------- |
| VcXsrv    | 2.x    | `xtigervncviewer` | 뷰어 창 크기에 가상 화면이 따라간다 |
| Xming 6.9 | 1.x    | `xtightvncviewer` | 고정. `-g WxH`로 지정               |

- `xtigervncviewer`는 XInput 2가 없으면 `X Input 2 (or newer) is not available`로 끝난다. 그래서 XInput 1.x 서버에는 `xtightvncviewer`를 쓴다.
- `-g WxH`는 attach 전에 세션 화면 크기를 바꾼다. `xtigervncviewer`는 그 뒤에도 창 크기를 따라간다.
- `xtigervncviewer` 안에서 `F8`을 누르면 뷰어 메뉴가 열린다 (전체 화면, 종료 등).
- 클립보드: `xtigervncviewer`의 클립보드 옵션(`SendClipboard`, `AcceptClipboard`)은 기본으로 켜져 있다. Windows까지 이어지는지는 X 서버의 클립보드 설정에 달렸고, 아직 확인하지 않았다.

## 환경 변수

| 변수               | 의미                                       | 기본값                       |
| ------------------ | ------------------------------------------ | ---------------------------- |
| `VDESK_WM`         | `new`의 기본 창 관리자                     | `xmonad`                     |
| `VDESK_IMAGE`      | 세션 이미지                                | `vdesk:latest`               |
| `VDESK_CORE_IMAGE` | core 이미지                                | `vdesk-core:latest`          |
| `XDG_CONFIG_HOME`  | 설정 디렉터리 (`$XDG_CONFIG_HOME/vdesk`)   | `~/.config`                  |
| `XDG_STATE_HOME`   | 세션 이름 디렉터리                         | `~/.local/state`             |
| `XDG_RUNTIME_DIR`  | 호스트 앱 인증, 로그, 중계 로그            | 없으면 `/tmp`                |
| `VISUAL`, `EDITOR` | `menu edit`의 편집기                       | `vi`                         |

개발 중인 worktree를 기존 이미지와 나란히 시험할 때는 이미지 이름을 바꾼다.

```bash
VDESK_IMAGE=vdesk:dev VDESK_CORE_IMAGE=vdesk-core:dev ./vdesk build
VDESK_IMAGE=vdesk:dev VDESK_CORE_IMAGE=vdesk-core:dev ./vdesk new -s dev
```

## 데이터 위치

| 데이터                 | 위치                                                  |
| ---------------------- | ----------------------------------------------------- |
| 세션 홈 (앱 설정 전체) | Docker volume `vdesk-<user>-home-<id>`                |
| 다운로드               | 호스트 `~/Downloads` (모든 세션이 공유)               |
| 세션 이름              | `~/.local/state/vdesk/<hostname>/<id>.name`           |
| 개인 설정 파일         | `~/.config/vdesk/home/` (세션 홈과 같은 구조)         |
| 개인 설정 복사 기록    | `~/.local/state/vdesk/<hostname>/<id>.home`           |
| addon 설정             | `~/.config/vdesk/addons.enabled`, `addons/<name>/`    |
| 메뉴 규칙              | `~/.config/vdesk/menu.conf`                           |
| 전체 배경화면          | `~/.config/vdesk/wallpaper.conf`, `wallpaper.img`     |
| 세션별 배경화면        | 세션 홈의 `~/.config/vdesk/wallpaper.local.*`         |
| `run --host` 인증/로그 | `$XDG_RUNTIME_DIR/vdesk-<uid>/<id>.xauth`, `<id>.log` |
| 호스트 중계 로그       | `$XDG_RUNTIME_DIR/vdesk-<uid>/hostd.log`              |

- 홈이 NFS라서 세션 홈은 로컬 디스크의 volume에 둔다. Chromium 계열 프로필의 SQLite 잠금 문제를 피하기 위해서다.
- 세션 이름은 호스트별 디렉터리에 둔다. NFS 홈을 공유하는 다른 호스트와 섞이지 않게 하기 위해서다.

## 문제 해결

- `DISPLAY is not set`: X11 forwarding 없이 접속했다. [접속 준비](#접속-준비)를 본다.
- `image vdesk:latest not found`: `vdesk build`를 먼저 실행한다.
- `session N did not come up`: `docker logs vdesk-<user>-N`으로 Xvnc 오류를 확인한다.
- 메뉴의 호스트 항목을 눌렀더니 오류 창이 뜬다: `vdesk hostd`로 중계가 떠 있는지 보고, 없으면 `vdesk hostd start`. 라벨이 호스트 `menu.conf`에 `LABEL = @COMMAND`로 있는지 확인한다.
- 호스트 wezterm 창이 검게 나온다: MIT-SHM을 끄기 전에 만든 세션이다. `vdesk recreate`로 옮긴다.
- addon을 켰는데 세션에 앱이 없다: `vdesk addon ls`에서 `BUILT`를 확인하고 `vdesk recreate`로 새 이미지로 옮긴다.
- Edge가 `Zygote process exited`로 죽는다: 이미지 label `vdesk.seccomp`가 비어 있다. `edge` addon을 켜고 빌드했는지 확인한다.
- 배경화면이 안 보인다: 배경화면 기능 이전 이미지의 세션이면 `vdesk recreate`. xmonad 세션은 창 사이 6px 간격과 빈 작업 공간에서 보인다. 간격 기능 이전 이미지의 xmonad 세션은 창이 화면을 덮는다.
- `Alt+Tab`이 세션으로 가지 않는다: Windows가 가로챈다. xmonad에서는 `Alt+j`/`Alt+k`를 쓴다.
- 한글이 입력되지 않는다: `vdesk addon ls`에서 kime가 `BUILT`인지, 세션이 그 이미지로 `recreate`됐는지 확인한다. 세션 안에서 `pgrep -a kime`로 데몬을 보고, 한/영 키는 [한글 입력](#한글-입력)의 `xev` 방법으로 확인한다.
- `home files not updated`: `~/.config/vdesk/home` 안의 symlink가 없는 곳을 가리키거나 읽을 수 없는 파일이 있다. 고친 뒤 `vdesk home sync -t TARGET`.
