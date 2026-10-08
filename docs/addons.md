# addon 작성 가이드

addon은 이미지에 앱을 추가하는 디렉터리 하나다. `vdesk build`가 core 이미지 위에 켜진 addon을 순서대로 하나씩 레이어로 적용한다.

## 구조

```
<name>/
├── addon.conf     # 필수. 설명, apt 패키지, 런타임 요구사항
├── pre-install    # 선택. 패키지 설치 전 실행 (apt 저장소 등록 등)
├── post-install   # 선택. 패키지와 rootfs 적용 후 실행 (캐시 갱신 등)
├── rootfs/        # 선택. 이미지의 / 에 그대로 복사
└── menu.conf      # 선택. 기본 메뉴 규칙 (docs/menu.md 문법)
```

- 이름: 영문 소문자나 숫자로 시작하고 `a-z 0-9 _ . -`만 쓴다.
- `pre-install`, `post-install`은 `sh -eu`로 root 권한에서 실행된다. 실행 권한 비트는 없어도 된다.
- 빌드 중에는 네트워크를 쓸 수 있다. core에 `curl`, `gpg`, `ca-certificates`가 있다.

## addon.conf

shell 변수 파일이다. 빌드할 때와 `vdesk addon ls`가 `source`한다.

| 변수          | 필수 | 의미                                                                    |
| ------------- | ---- | ----------------------------------------------------------------------- |
| `DESCRIPTION` | 권장 | `vdesk addon ls`에 표시할 설명                                          |
| `PACKAGES`    | 선택 | 공백으로 구분한 apt 패키지. `--no-install-recommends`로 설치            |
| `SECCOMP`     | 선택 | `unconfined`면 세션 컨테이너를 seccomp 없이 실행                        |
| `SHM_SIZE`    | 선택 | 필요한 `/dev/shm` 크기 (`512m`, `1g`). 켜진 addon 중 가장 큰 값을 쓴다  |

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

호스트에 이미 설치된 앱은 addon 없이 `vdesk run -t TARGET --host CMD`로 세션 화면에 띄운다. 격리되지 않으므로, 호스트에 설치된 대형 도구를 그대로 써야 할 때만 쓴다.

## 문제 해결

- 빌드 로그: `vdesk build > /tmp/$USER/vdesk-build.log 2>&1` 후 `RUN ... vdesk-apply-addon <name>` 단계를 확인한다.
- addon을 켰는데 세션에 앱이 없음: `vdesk addon ls`에서 `BUILT`를 확인하고, `vdesk recreate`로 세션을 새 이미지로 옮겼는지 확인한다.
- 메뉴에 안 나옴: 앱이 `.desktop`을 설치하지 않거나 `NoDisplay=true`인 경우다. addon의 `menu.conf`에 `Label = command`를 추가한다.
- Chromium 계열 앱이 `Zygote process exited`로 죽음: addon에 `SECCOMP=unconfined`가 필요하다. `--no-sandbox`로 우회하지 않는다.
