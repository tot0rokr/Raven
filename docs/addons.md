# addon 작성 가이드

addon은 이미지에 앱을 추가하는 디렉터리 하나다. `vdesk build`가 core 이미지 위에 켜진 addon을 순서대로 하나씩 레이어로 적용한다.

## 구조

```
<name>/
├── addon.conf     # 필수. 설명, apt 패키지, 런타임 요구사항
├── pre-install    # 선택. 패키지 설치 전 실행 (apt 저장소 등록 등)
├── post-install   # 선택. 패키지와 rootfs 적용 후 실행 (캐시 갱신 등)
├── rootfs/        # 선택. 이미지의 / 에 그대로 복사 (세션 hook은 etc/vdesk/session.d/, 작업표시줄 버튼은 etc/vdesk/panel.d/)
└── menu.conf      # 선택. 기본 메뉴 규칙 (docs/menu.md 문법)
```

- 이름: 영문 소문자나 숫자로 시작하고 `a-z 0-9 _ . -`만 쓴다.
- `pre-install`, `post-install`은 `sh -eu`로 root 권한에서 실행된다. 실행 권한 비트는 없어도 된다.
- 빌드 중에는 네트워크를 쓸 수 있다. core에 `curl`, `gpg`, `ca-certificates`가 있다.

## addon.conf

shell 변수 파일이다. 빌드할 때와 `vdesk addon ls`가 `source`한다.

| 변수          | 필수 | 의미                                                                       |
| ------------- | ---- | -------------------------------------------------------------------------- |
| `DESCRIPTION` | 권장 | `vdesk addon ls`에 표시할 설명                                             |
| `PACKAGES`    | 선택 | 공백으로 구분한 apt 패키지. `--no-install-recommends`로 설치               |
| `SECCOMP`     | 선택 | `unconfined`면 세션 컨테이너를 seccomp 없이 실행                           |
| `SHM_SIZE`    | 선택 | 필요한 `/dev/shm` 크기 (`512m`, `1g`). 켜진 addon 중 가장 큰 값을 쓴다     |
| `SESSION_ENV` | 선택 | 세션의 모든 프로세스에 넣을 환경 변수. 공백으로 구분한 `NAME=VALUE`        |

- `PACKAGES`에는 `.deb` 파일 경로도 쓸 수 있다. `pre-install`에서 받은 파일을 apt가 의존성과 함께 설치한다.
- `SESSION_ENV`는 이미지의 `ENV`가 된다. 시작 스크립트, 메뉴로 띄운 앱, `vdesk run`, `vdesk shell`이 모두 받는다. 값에 공백, 따옴표, 역슬래시, `$`를 쓸 수 없다.

## 적용 순서

addon 하나는 이미지 레이어 하나로 적용된다 (`vdesk-apply-addon`).

1. `pre-install` 실행
2. `apt-get update` 후 `PACKAGES` 설치
3. `rootfs/`를 `/`에 복사 (파일 모드 유지)
4. `menu.conf`를 `/etc/vdesk/menu.d/50-<name>.conf`로 복사
5. `post-install` 실행
6. apt 목록 정리

- addon은 `addons.enabled`에 적힌 순서대로 쌓인다. 앞쪽 addon을 바꾸면 그 뒤 레이어는 모두 다시 빌드된다. 자주 바꾸는 addon을 뒤에 둔다.
- `SECCOMP`, `SHM_SIZE`는 빌드할 때 이미지 label(`vdesk.seccomp`, `vdesk.shm-size`)로 기록되고, 세션을 만들 때(`new`, `recreate`) 적용된다.

## 세션 hook

입력기처럼 세션과 함께 떠 있어야 하는 데몬은 `rootfs/etc/vdesk/session.d/<순서>-<name>`에 셸 스크립트로 둔다. 세션 사용자 권한으로 `sh <hook> <이벤트>`가 이름순으로 실행된다.

| 이벤트   | 언제                                                                                   |
| -------- | -------------------------------------------------------------------------------------- |
| `start`  | 세션 화면이 뜬 직후. 창 관리자와 시작 명령보다 먼저                                    |
| `reload` | vdesk가 `~/.config/vdesk/home`에서 바뀐 파일을 세션 홈에 복사한 뒤 (실행 중인 세션만)  |

- hook은 데몬을 백그라운드로 띄우고 바로 끝낸다. 시작 명령은 hook이 끝난 뒤 시작한다.
- `reload`에서는 자기 설정이 실제로 바뀌었는지 보고 필요할 때만 다시 띄운다. 모르는 이벤트는 무시한다.
- `DISPLAY`가 설정되어 있다. 실패해도 세션은 계속 뜬다.

## 작업표시줄 버튼

`rootfs/etc/vdesk/panel.d/<순서>-<name>.tint2rc`에 tint2의 버튼 설정(`button = new`로 시작하는 묶음)을 둔다. `vdesk-panel`이 작업표시줄을 띄울 때 창 관리자별 기본 설정 뒤에 이 파일들을 이름순으로 붙이고, 버튼 수만큼 `panel_items`의 시계 앞에 `P`를 넣는다.

```
# rootfs/etc/vdesk/panel.d/50-kime.tint2rc
button = new
button_text = 한/A
button_tooltip = 한/영 전환 (kime)
button_lclick_command = vdesk-kime-toggle
button_font = Noto Sans KR Bold 9
button_font_color = #dddddd 100
button_padding = 10 2
button_background_id = 2
button_centered = 1
```

- 배경 번호는 core 설정의 것을 쓴다. 2번이 버튼 모양이다(xmonad, openbox 설정 모두).
- 세션 홈에 `~/.config/tint2/tint2rc`가 있으면 그 파일만 쓰므로 addon 버튼은 붙지 않는다.
- 설정 항목은 [tint2 매뉴얼](https://gitlab.com/o9000/tint2/-/blob/master/doc/tint2.md)의 Button.

## 위치와 우선순위

| 위치                             | 용도                              |
| -------------------------------- | --------------------------------- |
| `~/vdesk/addons/<name>/`         | 기본 제공 addon (프로젝트에 포함) |
| `~/.config/vdesk/addons/<name>/` | 사용자 addon. 같은 이름이면 우선  |

켜진 목록은 `~/.config/vdesk/addons.enabled`에 한 줄에 하나씩 적는다. `#` 주석과 빈 줄은 무시한다. 파일이 없으면 `edge`만 켜진 것으로 보고, 파일이 비어 있으면 addon 없이 빌드한다.

## 관리 명령

```bash
vdesk addon ls                  # NAME, ENABLED, BUILT(현재 이미지에 포함), SOURCE, DESCRIPTION
vdesk addon new NAME [PKG...]   # ~/.config/vdesk/addons/NAME/addon.conf 생성
vdesk addon enable NAME
vdesk addon disable NAME
vdesk build                     # core + 켜진 addon
vdesk recreate -a               # 실행 중인 세션을 새 이미지로 교체
```

`ENABLED`와 `BUILT`가 다르면 아직 빌드하지 않은 변경이 있는 것이다.

## 예시

### apt 패키지 하나

```bash
vdesk addon new gtkwave gtkwave
vdesk addon enable gtkwave
vdesk build && vdesk recreate -a
```

생성되는 `addon.conf`:

```sh
DESCRIPTION="gtkwave"
PACKAGES="gtkwave"
```

apt 패키지가 `.desktop` 파일을 함께 설치하면 메뉴에 자동으로 나온다.

### 외부 apt 저장소가 필요한 앱

기본 제공 `edge` addon이 이 형태다 (`~/vdesk/addons/edge/`).

```sh
# addon.conf
DESCRIPTION="Microsoft Edge (stable)"
PACKAGES="microsoft-edge-stable"
SECCOMP=unconfined
SHM_SIZE=1g
```

```sh
# pre-install
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /usr/share/keyrings/microsoft-edge.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main" \
    > /etc/apt/sources.list.d/microsoft-edge.list
```

### 저장소에 없는 .deb와 세션 데몬

기본 제공 `kime` addon이 이 형태다 (`~/vdesk/addons/kime/`). upstream이 배포하는 `.deb`를 버전과 sha256을 고정해 받고, 입력기 환경 변수와 데몬 hook을 둔다.

```sh
# addon.conf
DESCRIPTION="kime Korean input method"
PACKAGES="/tmp/kime.deb xdotool"
SESSION_ENV="GTK_IM_MODULE=kime QT_IM_MODULE=kime XMODIFIERS=@im=kime"
```

```sh
# pre-install
version=3.2.0
sha256=05f74236414345aa77047fd5f490d7383a8db2cd1cd8550a7dc47b06bd755b51
curl -fsSL -o /tmp/kime.deb \
    "https://github.com/Riey/kime/releases/download/v$version/kime-v${version}_amd64_ubuntu-24.04.deb"
echo "$sha256  /tmp/kime.deb" | sha256sum -c -
```

- `post-install`에서 `/tmp/kime.deb`를 지운다.
- `rootfs/etc/vdesk/session.d/50-kime`: `start`에서 kime를 띄우고 XIM 서버가 등록될 때까지 기다린다. `reload`에서는 `~/.config/kime`가 바뀌었을 때만 다시 띄운다.
- `rootfs/etc/vdesk/panel.d/50-kime.tint2rc`: 작업표시줄의 `한/A` 버튼. 누르면 `vdesk-kime-toggle`이 포커스된 창에 `Hangul` 키를 보낸다(`xdotool`).

### 실행 옵션 고정

`rootfs/usr/local/bin/`에 같은 이름의 wrapper를 두면 PATH에서 원래 실행 파일보다 먼저 잡힌다. 메뉴 항목도 wrapper를 쓰게 하려면 같은 이름의 `.desktop`을 `rootfs/usr/local/share/applications/`에 둔다. 같은 파일 이름이면 `/usr/share/applications/`의 것보다 우선한다.

```
edge/rootfs/usr/local/bin/microsoft-edge                        # 기본 플래그를 붙여 /usr/bin/microsoft-edge 실행
edge/rootfs/usr/local/share/applications/microsoft-edge.desktop # Exec=microsoft-edge %U
```

### 폰트

```sh
# addon.conf
DESCRIPTION="Nanum fonts"
PACKAGES="fonts-nanum"
```

폰트 파일을 `rootfs/usr/local/share/fonts/`로 직접 넣는 경우에는 `post-install`에 `fc-cache -f`를 적는다.

### 호스트 앱을 쓰는 경우

호스트에 이미 설치된 앱은 addon 없이 `vdesk run -t TARGET --host CMD`로 세션 화면에 띄운다. 메뉴에 넣으려면 `menu.conf`에 `LABEL = @COMMAND`로 적는다 ([menu.md](./menu.md)). 격리되지 않으므로, 호스트에 설치된 대형 도구를 그대로 써야 할 때만 쓴다.

## 문제 해결

- 빌드 로그: `vdesk build > /tmp/$USER/vdesk-build.log 2>&1` 후 `RUN ... vdesk-apply-addon <name>` 단계를 확인한다.
- addon을 켰는데 세션에 앱이 없음: `vdesk addon ls`에서 `BUILT`를 확인하고, `vdesk recreate`로 세션을 새 이미지로 옮겼는지 확인한다.
- 메뉴에 안 나옴: 앱이 `.desktop`을 설치하지 않거나 `NoDisplay=true`인 경우다. addon의 `menu.conf`에 `Label = command`를 추가한다.
- Chromium 계열 앱이 `Zygote process exited`로 죽음: addon에 `SECCOMP=unconfined`가 필요하다. `--no-sandbox`로 우회하지 않는다.
