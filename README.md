# vdesk

tmux처럼 쓰는 지속형 X 데스크톱. 세션마다 Docker 컨테이너 안에 가상 화면(TigerVNC `Xvnc`)과 openbox를 띄우고, 뷰어를 X11(`ssh -Y`)로 클라이언트 X 서버에 띄운다. 뷰어를 닫거나 SSH가 끊겨도 세션 안의 앱은 계속 실행된다. 앱은 addon으로 이미지에 추가하고, 호스트에는 Docker 외 패키지를 설치하지 않는다.

```
image vdesk:latest = vdesk-core (Xvnc, openbox, tint2, viewers, xterm) + enabled addons (edge, ...)

┌─ session 0 (:100, 15900) ──────┐   ┌─ session 1 (:101, 15901) ──────┐
│ Xvnc + openbox                 │   │ Xvnc + openbox                 │
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
vdesk                                  # 새 세션(xterm) 생성 후 attach
vdesk new -s edge microsoft-edge       # 이름과 시작 명령 지정
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
- 재부팅 뒤 추가로 띄울 앱은 세션 홈의 `~/.config/openbox/autostart`에 `app &` 형식으로 적는다 (`vdesk shell`로 편집).

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

세션 바탕화면을 우클릭하면 메뉴가 뜬다. 메뉴는 열 때마다 규칙 파일과 설치된 앱의 `.desktop` 파일로 새로 만들어진다.

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

세션 화면 아래쪽에 [tint2](https://gitlab.com/o9000/tint2) 작업표시줄이 있다. 왼쪽은 열린 창 목록, 오른쪽은 날짜와 시각이다.

- 창 버튼을 누르면 최소화와 복원이 바뀐다. 최소화한 창은 여기서 되살린다.
- 최대화한 창은 작업표시줄 위까지만 커진다.
- 기본 설정은 이미지의 `/etc/xdg/tint2/tint2rc`다. 세션 홈에 `~/.config/tint2/tint2rc`가 있으면 그 파일을 쓴다. `vdesk shell`에서 `mkdir -p ~/.config/tint2 && cp /etc/xdg/tint2/tint2rc ~/.config/tint2/`로 복사한 뒤 고친다. 설정 항목은 [tint2 매뉴얼](https://gitlab.com/o9000/tint2/-/blob/master/doc/tint2.md).
- 세션 홈의 설정을 바꾼 뒤에는 `vdesk shell`에서 `pkill -x tint2; pkill -x openbox`로 다시 띄운다. openbox는 entrypoint가 다시 띄우고, 이때 tint2도 함께 뜬다.

## 이미지 갱신

- 이미지를 다시 빌드해도 실행 중인 세션은 이전 이미지를 계속 쓴다. `vdesk recreate TARGET` 또는 `vdesk recreate -a`로 옮긴다.
- `recreate`는 컨테이너만 교체하고 홈 volume, 이름, 시작 명령, 화면 번호, 포트는 유지한다. 세션 안의 앱은 다시 시작된다.
- Edge 등 패키지 업데이트: `vdesk build --no-cache` 후 `vdesk recreate -a`. `--pull`은 core의 base 이미지에만 적용된다.

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

- 홈이 NFS라서 세션 홈은 로컬 디스크의 volume에 둔다. Chromium 계열 프로필의 SQLite 잠금 문제를 피하기 위해서다.
- 세션 이름은 호스트별 디렉터리에 둔다. NFS 홈을 공유하는 다른 호스트와 섞이지 않게 하기 위해서다.

## 보안 설계

- `--network host`: 뷰어가 SSH X11 포워딩 포트(`127.0.0.1:60xx`)에 닿고, `run --host` 앱이 세션의 X socket(abstract socket)에 닿기 위해 쓴다.
- 화면 번호는 100부터, VNC 포트는 15900부터 비어 있는 것을 고른다. VNC는 `127.0.0.1`에만 열리고, 비밀번호는 세션이 시작할 때마다 새로 만들어 컨테이너 밖으로 꺼내지 않는다.
- X 화면은 host network namespace에 노출되므로 MIT-MAGIC-COOKIE 인증을 건다.
- seccomp, `/dev/shm` 크기는 addon이 요구할 때만 바꾼다. 켜진 addon 중 하나라도 `SECCOMP=unconfined`면 모든 세션에 적용된다 (`edge`가 Chromium sandbox 때문에 요구).
