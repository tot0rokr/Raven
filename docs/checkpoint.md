# 디스크 checkpoint 검증 (M11)

세션 컨테이너의 실행 상태를 Docker checkpoint(CRIU)로 디스크에 저장하고, 컨테이너가 멈췄거나 서버가 재부팅된 뒤 복원할 수 있는지 시험한 기록이다. 기능 구현([M12](./roadmap.md#m12--검증한-checkpoint의-저장복원-기능)) 전의 판정 자료다.

판정: **M12 보류.** Xvnc·창 관리자·D-Bus·PCManFM·kime 세션은 조건을 맞추면 저장되고, 재부팅을 흉내 낸 시험 컨테이너 재시작 뒤에도 복원된다. 그러나 기본 addon인 Microsoft Edge는 sandbox가 만드는 중첩 PID namespace 때문에 저장할 수 없고, 운영 서버의 공유 Docker 데몬과 패키지를 바꿔야 한다. 근거와 다시 볼 조건은 [M12 판정](#m12-판정).

## 시험 환경

운영 서버는 바꾸지 않았다. 같은 서버에 privileged 시험 컨테이너(lab)를 띄우고, 그 안에서 별도 dockerd를 experimental로 실행했다(Docker-in-Docker). Docker·containerd·runc는 운영 서버와 같은 Ubuntu 패키지이고 커널은 서버 커널을 그대로 쓴다.

| 항목            | 운영 서버                                       | lab                                                                       |
| --------------- | ----------------------------------------------- | ------------------------------------------------------------------------- |
| OS              | Ubuntu 24.04.1                                  | Ubuntu 24.04.5 (컨테이너 이미지)                                          |
| 커널            | 6.8.0-149-generic                               | 같은 커널                                                                 |
| Docker          | `docker.io` 29.1.3-0ubuntu3~24.04.2             | 같은 패키지                                                               |
| experimental    | 꺼짐                                            | 켬 (`/etc/docker/daemon.json`)                                            |
| containerd      | 2.2.1-0ubuntu1~24.04.3                          | 같은 패키지                                                               |
| runc            | 1.3.4-0ubuntu1~24.04.1                          | 같은 패키지                                                               |
| CRIU            | 없음 (Ubuntu 24.04 archive에 없음)              | 4.3.1 (upstream `v4.3.1`, `3ce556b` 소스 빌드)                            |
| 이미지 저장소   | containerd snapshotter `overlayfs`              | 같음. lab의 `/var/lib/docker`는 ext4 named volume                         |
| cgroup          | v2, systemd 드라이버                            | v2, cgroupfs 드라이버 (lab 안에 systemd 없음)                             |
| AppArmor        | 세션에 `docker-default`                         | 서버에 이미 로드된 `docker-default`를 세션에 적용. securityfs를 읽기 전용으로 붙이고 `apparmor_parser`는 거부만 하는 stub이라 lab은 정책을 올리지 못함 |
| seccomp         | `raven:latest`의 label로 `unconfined`           | 같음                                                                      |
| `--network host` | 서버의 network namespace                       | lab 컨테이너의 network namespace                                          |
| `~/Downloads`   | NFSv3 홈                                        | NFSv3 (`~/.cache/raven-lab-m11-*/Downloads`)                              |
| `/run`          | tmpfs                                           | tmpfs                                                                     |
| 세션 이미지     | `raven:latest` (edge kime files), `raven-core:latest` | 같은 이미지 (`docker save` → `docker load`)                          |

## 방법

- 세션은 저장소의 `raven` CLI(읽기 전용 mount)를 lab 안에서 실행해 만들었다. CLI를 실행한 lab 사용자는 서버 사용자와 uid·gid가 같고 HOME·XDG 디렉터리는 시험용이다. 그래서 세션 컨테이너는 `run_container`의 옵션을 그대로 가진다: `--init`, `--network host`, `--restart unless-stopped`, 메모리 2g·swap 차단·CPU 2·pids 512, `raven.seccomp`·`raven.shm-size` label, uid 60000, `--group-add`, 홈 named volume, NFS `Downloads` bind mount, `TZ`.
- CRIU 옵션은 runc가 checkpoint와 restore마다 읽는 `/etc/criu/runc.conf`로 넘겼다. 시도마다 파일을 다시 썼다. CRIU 로그는 같은 파일의 `log-file`로 모았다.
- 앱은 메뉴처럼 세션의 init(`docker-init`) 아래에서 실행했다(`setsid`로 띄우고 부모가 끝나 init으로 넘어감). `raven run`으로 띄운 프로세스는 따로 확인했다([실행 경로](#실행-경로)).
- 크기는 checkpoint 디렉터리의 파일 크기 합, 시간은 `docker checkpoint create`와 `docker start --checkpoint`의 벽시계 시간이다.
- 재부팅은 lab 컨테이너 재시작으로 흉내 냈다. lab 안의 모든 프로세스가 끝나고 `/run`(tmpfs)이 비워지며 `/var/lib/docker`만 남는다. 서버 커널은 재부팅하지 않았다.
- 복원 확인 항목:
  - VNC: 세션의 VNC 비밀번호로 웹 중계처럼 RFB 인증을 마치고 화면 갱신(FramebufferUpdate)을 받는다.
  - 창: `xwininfo -root -children`의 창 id와 이름 목록이 저장 전과 같다.
  - RAM: 세션 안 python3 프로세스의 64 MiB marker가 같은 PID, 같은 token, 같은 sha256이고 counter가 저장 시점부터 이어진다(새로 시작하면 0부터).
  - 미저장 입력: bash(readline)에 Enter 없이 친 명령과 `read`(canonical tty)에 친 줄을 복원 뒤 Enter로 제출한다. 입력은 VNC 키 이벤트로 보냈고, 같은 경로로 Enter까지 보낸 대조 입력이 전달되는 것도 확인했다.
  - 새 X 클라이언트 접속, D-Bus 세션 버스 호출, 복원된 프로세스의 AppArmor label.

재현:

```bash
python3 tests/checkpoint_lab/run_lab.py            # 끝나면 lab 컨테이너·volume·임시 디렉터리를 지운다
python3 tests/checkpoint_lab/run_lab.py --keep     # 디버깅용으로 남긴다
```

lab 이미지(`raven-lab-m11:latest`)는 [tests/checkpoint_lab/Dockerfile](../tests/checkpoint_lab/Dockerfile)로 만들고 남겨 둔다. 단계별 판단은 [ladder.py](../tests/checkpoint_lab/ladder.py)에 있다. 요약 JSON은 표준 출력과 `--out` 디렉터리의 `summary.json`, CRIU 로그는 같은 디렉터리의 `criu-logs/`에 남는다. 한 번 실행에 약 5분 걸린다(lab 이미지가 없으면 빌드에 약 3분 더).

## 결과

수치는 전체 실행 한 번(저장소 `96d5817`, `raven:latest` `5e640275b73f`)의 값이다. 같은 스크립트를 다시 실행했을 때 성공·실패는 같았고 시간과 크기는 10% 안에서 달랐다. 크기는 저장 직전 세션 cgroup의 `memory.current`와 거의 같다. 저장하면 컨테이너가 멈추고 cgroup이 사라지므로 그만큼 RAM이 회수된다.

| 단계   | 구성                                                                    | 결과                      | 크기    | 저장   | 복원  | 필요한 옵션·조치                                                         |
| ------ | ----------------------------------------------------------------------- | ------------------------- | ------- | ------ | ----- | ------------------------------------------------------------------------ |
| 1      | python3 64 MiB marker, `--network host`                                 | 성공                      | 68 MiB  | 2.3 s  | 1.4 s | 없음                                                                     |
| 1      | 같은 프로세스, 기본 bridge·`--network none`                            | 실패 (복원)               | 68 MiB  | 2.2 s  | -     | 없음 (Docker 결함)                                                       |
| 1      | `run_container`와 같은 옵션 (named volume, NFS Downloads, `--init`, 제한) | 성공                    | 68 MiB  | 1.9 s  | 1.5 s | 복원에 `mntns-compat-mode`, 재시작 정책 `no`                             |
| 1      | `--leave-running`으로 저장, 계속 실행한 뒤 그 시점으로 복원           | 성공                      | 68 MiB  | 2.1 s  | 1.5 s | 위와 같음                                                                |
| 1      | 원래 컨테이너를 지우고 같은 옵션의 새 컨테이너로                       | 성공                      | 68 MiB  | 2.2 s  | 1.6 s | checkpoint 파일을 새 컨테이너의 checkpoint 디렉터리로 복사               |
| 2      | Xvnc + xmonad + D-Bus + tint2 + xterm (`raven-core:latest`)            | 성공                      | 100 MiB | 3.4 s  | 2.1 s | tint2 끄기, 복원에 `mntns-compat-mode`                                   |
| 2      | Xvnc + openbox + D-Bus + tint2 + xterm (`raven-core:latest`)           | 성공                      | 102 MiB | 3.7 s  | 2.1 s | 위와 같음                                                                |
| 3      | 2단계(xmonad) + PCManFM + kime (`raven:latest`)                         | 성공                      | 170 MiB | 5.0 s  | 3.2 s | 위와 같음 + dump·restore에 `file-locks`                                  |
| 재부팅 | 3단계 구성을 저장, lab 재시작 뒤 복원                                   | 성공                      | 170 MiB | 5.4 s  | 3.2 s | 3단계와 같음                                                             |
| 4      | Edge (sandbox 켬) + kime                                                | 실패 (저장)               | -       | -      | -     | 우회하는 옵션 없음. 저장 직전 메모리 337 MiB, Edge 프로세스 20개         |
| 참고   | Edge `--no-sandbox` + kime                                              | 성공 (지원 구성 아님)     | 297 MiB | 10.5 s | 6.3 s | `file-locks`, `tcp-established`                                          |

사다리는 4단계에서 멈췄다. 2·3단계와 재부팅 뒤 복원에서 확인한 것:

| 항목                                          | 결과                                                                       |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| VNC 인증과 화면 갱신                          | 됨                                                                         |
| 창 id·이름 목록                               | 저장 전과 같음                                                             |
| RAM marker                                    | 같은 PID·token·sha256, counter가 이어짐 (130 → 150)                       |
| bash에 Enter 없이 친 명령                     | 복원 뒤 Enter로 실행됨                                                     |
| `read`에 Enter 없이 친 줄 (canonical tty)     | 사라짐                                                                     |
| `raven run`으로 띄운 프로세스                 | 사라짐                                                                     |
| 새 X 클라이언트, D-Bus 호출                   | 됨                                                                         |
| AppArmor label                                | `docker-default (enforce)`로 복원                                          |
| PCManFM                                       | 같은 PID, `Ctrl+N`에 새 창                                                 |
| kime                                          | 같은 PID, root 창 `XIM_SERVERS`에 `@server=kime`                           |
| 재부팅 뒤                                     | 세션은 `exited`, checkpoint 남음. 복원 뒤 `raven ls`에 `running` 172MiB    |

2단계 xmonad 세션에서 본 추가 상황:

| 상황                                              | 결과                                                                                 |
| ------------------------------------------------- | ------------------------------------------------------------------------------------ |
| VNC 뷰어가 TCP로 붙어 있음                        | 저장 실패. `tcp-established`를 주면 저장·복원되고 뷰어가 이어서 화면을 받음          |
| 컨테이너 밖 X 클라이언트 (`run --host`와 같음)    | 저장 실패. `ext-unix-sk`를 줘도 실패                                                 |
| 복원 전 VNC 포트 또는 X abstract socket을 다른 프로세스가 점유 | 복원 실패, 컨테이너는 멈춘 채 남음. 비운 뒤 복원 성공                   |
| 세션 프로세스가 연 `~/Downloads` 파일의 크기가 바뀜 | 복원 거부                                                                          |
| 같은 파일이 같은 크기로 바뀜                      | 복원됨. 복원된 프로세스가 바뀐 내용을 읽음                                           |
| group만 읽을 수 있는 NFS 파일(0640)을 연 프로세스 | 저장·복원 됨. 이 서버의 NFS에서 root(CRIU)가 그 파일을 읽을 수 있었다                |
| 원래 컨테이너를 지우고 새 컨테이너로              | 실패(`/tmp`의 socket 디렉터리 없음). 디렉터리를 만들어 주면 복원되고 X가 응답        |
| 재시작 정책 `unless-stopped` 그대로 저장          | Docker가 바로 새로 시작(재시작 1회). 재부팅 뒤에도 새로 시작한 상태                  |

## 실패 분석

각 항목은 CRIU 로그(`criu-logs/*.log`) 또는 Docker 응답의 발췌다.

### bridge·none 네트워크: Docker가 복원 전에 실패

```
Error response from daemon: bind-mount /proc/0/ns/net -> /var/run/docker/netns/239786bfddc4: no such file or directory
```

checkpoint는 되지만 `docker start --checkpoint`가 CRIU를 부르기 전에 실패한다. moby 29.1.3의 `daemon/start_linux.go`(`initializeCreatedTask`)는 새 network namespace를 `/proc/<task pid>/ns/net`으로 연결하는데, checkpoint에서 만든 task는 아직 시작 전이라 pid가 0이다. 자체 network namespace가 있는 컨테이너(기본 bridge, `--network none`)는 이 버전에서 복원할 수 없다. Raven 세션은 `--network host`라서 이 경로를 타지 않는다.

### 홈 named volume: mount-v2 엔진의 복원 실패

```
Error (criu/mount-v2.c:897): mnt-v2: Failed to copy sharing from -1:/var/lib/docker/volumes/m11-r1-vol/_data to 6: Invalid argument
Error (criu/cr-restore.c:2424): Restoring FAILED.
```

named volume mount는 바깥 mount의 slave(`master:1`)다. CRIU 4.3.1의 기본 mount 엔진(mount-v2)은 이 공유 관계를 복원하지 못한다. 예전 엔진(`mntns-compat-mode`)으로는 복원된다. 이 옵션은 dump에 있으면 거부된다.

```
Error (criu/config.c:1327): Option --mntns-compat-mode is only valid on restore
```

runc는 checkpoint와 restore에 같은 설정 파일을 쓰므로, 운영에서는 복원 직전에 설정을 바꾸거나 복원만 따로 실행하는 root 도우미가 필요하다. lab의 `/var/lib/docker`는 서버 root mount의 slave이고 운영 서버에서는 `/var/lib/docker`가 공유(shared) root mount 안에 있다. 운영 구조에서 mount-v2가 같은 오류를 내는지는 확인하지 않았다.

### tint2: kernel uevent socket

```
Error (criu/sk-netlink.c:77): netlink: The socket has data to read
Error (criu/cr-dump.c:1550): Dump files (pid: 904) failed with -1
```

작업표시줄 tint2는 `NETLINK_KOBJECT_UEVENT` socket을 연다. CRIU는 읽지 않은 메시지가 남은 netlink socket을 저장하지 못한다. dump 중에 uevent 수신기를 붙여 보니 `add@/fs/nfs/net/nfs_client`·`remove@...`가 3쌍 왔다. NFS client 모듈은 network namespace가 생기고 없어질 때마다 이 uevent를 낸다. 그 시간에 lab에서 돈 것은 checkpoint뿐이므로 checkpoint 과정(runc·CRIU)이 namespace를 만들고 지운 것으로 본다. 같은 구성에서도 결과가 갈렸다. 시험 중 xmonad 세션은 7번 중 5번 실패했고 openbox 세션은 4번 모두 성공했다. 위 발췌는 디버깅 실행의 로그다. 운영 서버의 host network namespace에는 다른 사용자의 컨테이너가 만드는 veth 같은 장치 uevent도 오므로 실패가 줄어들 근거가 없다. tint2를 끄고 저장한 뒤 복원하고 다시 띄우면 된다(`raven-panel`).

### kime: 파일 잠금

```
Error (criu/file-lock.c:112): Some file locks are hold by dumping tasks! You can try --file-locks to dump them.
```

kime가 `/tmp/kime.pid`에 `flock`을 잡고 있다. `file-locks`를 dump와 restore 모두에 주면 된다.

### Edge: 중첩 PID namespace

```
Error (criu/namespaces.c:462): Can't dump nested pid namespace for 12948
Error (criu/namespaces.c:702): Can't make pidns id
```

Chromium 계열의 sandbox zygote가 user·PID namespace를 새로 만든다. CRIU 4.3.1은 컨테이너 안에 중첩된 mount·network namespace만 다루고(`CLONE_SUBNS`), 중첩 PID·user namespace는 거부한다. 이를 우회하는 CRIU 옵션은 없다. `file-locks`, `tcp-established`, `ext-unix-sk`를 더해도 같다. sandbox를 끄는 `--no-sandbox`는 Raven에서 쓰지 않는 구성이다.

참고로 `--no-sandbox`로 띄운 Edge는 저장·복원됐다(지원 구성이 아님). 이때도 Edge가 외부 서버(443·80번 포트)와 TCP 연결 7개를 유지하고 있어 `tcp-established`가 필요했고, CRIU가 iptables 규칙 14개를 넣었다. 복원 뒤 같은 탭의 입력란에 이어서 친 글자가 저장 전 글자 뒤에 붙었다(창 제목 `m11:abc` → `m11:abcdef`).

### 실행 경로

`raven run`(웹의 앱 실행 포함)과 `raven shell`은 `docker exec`로 프로세스를 띄운다. 이 프로세스는 컨테이너 init의 자손이 아니다. runc는 init부터 프로세스 트리를 저장하므로 CRIU는 경고만 남기고 이들을 버린다.

```
Warn  (criu/seize.c:404): Unexpected process 3206 in the freezer cgroup (status 0x80057f)
```

복원 뒤 `raven run`으로 띄운 `sleep`은 없었다. 메뉴·작업표시줄·xmonad 키로 띄운 앱은 창 관리자의 자손이라 저장된다. 시작 명령(`new`의 CMD)도 저장된다.

### 재시작 정책

세션의 `--restart unless-stopped` 때문에 `docker checkpoint create`로 컨테이너가 멈추자마자 Docker가 새로 시작한다(재시작 횟수 1, 새 프로세스로 시작). 이 상태에서 checkpoint를 복원하려면 다시 멈춰야 하고, 그 사이 새로 뜬 세션의 시작 스크립트가 홈의 `~/.vnc/passwd`, `~/.Xauthority`를 새로 쓴다. 저장 전에 `docker update --restart no`로 정책을 끄면 멈춘 채로 남고 재부팅 뒤에도 자동 시작되지 않는다. `docker kill -s SIGNAL`은 신호와 관계없이 컨테이너를 "수동 정지"로 표시해 이 동작을 가린다.

### 열린 VNC 연결

```
Error (criu/sk-inet.c:201): inet: Connected TCP socket, consider using --tcp-established option.
```

뷰어가 붙어 있으면 저장되지 않는다. `tcp-established`를 주면 저장·복원되고 lab에서는 붙어 있던 뷰어가 이어서 화면을 받았다. 이때 CRIU는 연결을 잠그려고 세션의 network namespace(운영에서는 서버의 host network namespace)에 iptables 규칙을 넣는다.

```
Running iptables [iptables -w -t filter -I INPUT --protocol tcp -m mark ! --mark 0xC114 --source 127.0.0.1 --sport 40682 --destination 127.0.0.1 --dport 15900 -j DROP]
```

운영 서버의 방화벽 규칙을 건드리므로 이 옵션은 쓰지 않고, `raven pause`처럼 뷰어를 먼저 끊는다.

### 바깥의 X 클라이언트 (`run --host`)

컨테이너 밖 프로세스가 Xvnc의 abstract socket(`@/tmp/.X11-unix/X100`)에 연결해 두면 저장되지 않는다.

```
Error (criu/sk-unix.c:893): unix: External socket is used. Consider using --ext-unix-sk option.
Error (criu/sk-unix.c:900): unix: Can't dump half of stream unix connection. name: ; peer name: (null)
```

두 번째 줄은 `ext-unix-sk`를 준 결과다. stream 연결의 한쪽만은 저장할 수 없다. `run --host`로 띄운 앱이 세션 화면에 떠 있으면 저장 전에 끝내야 한다. Raven은 이런 연결을 `ss -xp`로 Xvnc 쪽 socket의 상대가 컨테이너 밖인지 보고 미리 거부할 수 있다.

### 포트와 화면 번호 충돌

Xvnc는 host network의 `127.0.0.1:15900`과 abstract socket `@/tmp/.X11-unix/X100`을 다시 bind한다. 복원 전에 다른 프로세스가 둘 중 하나를 잡고 있으면 복원이 실패한다.

```
Error (criu/sk-inet.c:1077): inet: Can't bind inet socket (id 68): Address already in use
Error (criu/sk-unix.c:1691): unix: Can't bind id 0x42 ino 61547041 addr : Address already in use
```

실패해도 컨테이너는 멈춘 채 남고 checkpoint도 그대로라서, 자원을 비운 뒤 다시 복원하면 된다.

### 저장 중에 바뀐 파일

CRIU는 열린 일반 파일의 경로와 크기만 기록하고 내용은 저장하지 않는다(`Only file size could be stored for validation for file ...`). 지운 채 열려 있는 파일과 `/dev/shm` 같은 tmpfs는 내용을 저장한다. 세션 프로세스가 연 `~/Downloads` 파일을 저장 뒤 바꾸면:

- 크기가 바뀌면 복원이 거부된다: `File home/raven/Downloads/m11-r2x-held.txt has bad size 47 (expect 22)`.
- 크기가 같으면 복원되고, 복원된 프로세스는 바뀐 내용을 읽는다(`CHANGED! content 1234`). 탐지되지 않는다.

홈 volume도 같다. checkpoint는 홈 volume과 공유 `~/Downloads`를 포함하지 않으므로 저장부터 복원까지 파일이 바뀌지 않게 막거나, 열린 파일 목록과 크기·mtime·hash를 따로 기록해 복원 전에 비교해야 한다.

### 새 컨테이너로 복원

`docker start --checkpoint-dir`는 Docker가 거부한다(`custom checkpointdir is not supported`, moby `daemon/start.go`). checkpoint 파일을 새 컨테이너의 checkpoint 디렉터리(`/var/lib/docker/containers/<id>/checkpoints/`)로 복사하면 받아들인다. 작은 프로세스는 이렇게 다른 컨테이너로 복원됐다. 세션은 처음에 실패했다.

```
Error (criu/sk-unix.c:1691): unix: Can't bind id 0x70 ino 61852741 addr /tmp/raven-bus/bus: No such file or directory
```

Docker checkpoint는 컨테이너의 쓰기 layer를 포함하지 않는다. 새 컨테이너에 `/tmp/.X11-unix`와 `/tmp/raven-bus` 디렉터리를 만들어 주니 복원되고 X가 응답했다. 다만 쓰기 layer의 다른 파일(`/tmp/raven-tint2rc`, kime 상태 파일, 앱이 `/tmp`에 둔 파일)은 없어진 채라서, 앱이 그 파일을 열고 있었다면 복원이 실패하고 나중에 읽는다면 동작이 달라진다. 세션은 원래 컨테이너에 복원하는 것을 기본으로 하고, checkpoint가 있는 세션은 `kill`·`recreate`·이미지 교체를 막는다.

### 터미널에 친 줄

bash(readline)에 Enter 없이 친 명령은 bash 메모리에 있어서 복원 뒤 Enter로 실행됐다. `read`처럼 canonical 모드 tty에 Enter 없이 친 줄은 커널 tty buffer에 있다가 복원 뒤 사라졌다(Enter를 보내면 빈 줄을 읽음). 같은 입력 경로의 대조 입력은 전달됐다.

## 운영 서버에 필요한 변경

lab에서 동작한 구성을 운영 서버에 그대로 옮기려면 아래가 모두 필요하다. 지금 운영 서버에는 하나도 적용되어 있지 않고, 이 시험에서도 바꾸지 않았다.

| 변경                               | 내용                                                                                                                         | 영향                                                                   |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| CRIU 설치                          | Ubuntu 24.04 archive에 없다. CRIU 팀 PPA(`ppa:criu/ppa`) 또는 소스 빌드. lab은 4.3.1 소스 빌드                               | 서버 패키지 추가 (현재 규칙상 금지)                                    |
| Docker experimental                | `/etc/docker/daemon.json`에 `"experimental": true`, dockerd 재시작                                                           | 공유 데몬이라 모든 사용자의 컨테이너에 영향. 재시작 중 컨테이너가 멈춤 |
| CRIU 설정                          | `/etc/criu/runc.conf`(root)에 `file-locks`. 복원 때만 `mntns-compat-mode`                                                    | 서버 전체의 모든 runc checkpoint에 적용됨                              |
| root 도우미                        | dump와 restore에 서로 다른 CRIU 설정을 쓰고, checkpoint 파일을 지우고 옮기는 일                                              | sudo 규칙이나 systemd 서비스 추가                                      |
| 디스크                             | `/var/lib/docker/containers/<id>/checkpoints/` 아래 세션당 100~200 MB (Edge 제외), 재부팅 뒤까지 유지                         | `/var/lib/docker` 용량                                                 |
| 부팅 순서                          | 재시작 정책을 끈 세션은 자동으로 뜨지 않는다. 부팅 뒤 `docker start --checkpoint`를 실행할 주체가 필요                      | 사용자 서비스 또는 수동 복원                                           |

AppArmor·seccomp 프로필은 바꿀 필요가 없었다. 세션 프로세스는 `docker-default`로 저장되고 같은 label로 복원됐다. CRIU는 runc가 root로 실행하므로 Raven 사용자에게 새 권한을 주지 않는다. 다만 checkpoint·restore 자체가 Docker API이므로 `docker` 그룹(사실상 root)이 전제다. runc가 위험한 annotation으로 분류하는 `org.criu.config`로 컨테이너마다 설정 파일을 지정하는 방법은 쓰지 않는다. 사용자가 쓸 수 있는 설정 파일을 root CRIU가 읽으면 `action-script`로 root 명령을 실행할 수 있다.

Raven에 필요한 변경(M12 범위):

- 저장 전: 재시작 정책을 `no`로, 뷰어 끊기(`raven pause`와 같음), tint2 끄기, `run --host` 앱의 X 연결과 컨테이너 밖 연결 거부, `docker exec`로 띄운 프로세스(`raven run`, `raven shell`) 경고 또는 거부, Edge 등 중첩 namespace를 쓰는 프로세스 거부.
- `raven run`과 웹의 앱 실행이 앱을 init 아래로 넘기게 바꾼다(`setsid ... &` 후 부모 종료). 바꾸지 않으면 그 앱들은 저장되지 않고 사라진다.
- 저장 뒤: checkpoint가 있는 세션에 `attach`, `run`, `home sync`, `menu sync`, `wallpaper`, `backup`, `restore`, `recreate`, `kill`을 막거나 확인을 받는다(파일 일관성, 쓰기 layer 보존).
- 복원 전: 화면 번호와 VNC 포트가 비어 있는지 확인. 복원 뒤: 재시작 정책 되돌리기, tint2 다시 띄우기, checkpoint 삭제.
- `raven ls`와 웹 목록에 "저장됨" 상태 표시. 지금은 `exited`로만 보인다.

### 저장 전 탐지와 거부

지원하지 않는 구성은 CRIU가 실패하기 전에 서버에서 찾을 수 있다. 모두 세션 cgroup의 `cgroup.procs`와 `/proc`, `ss`를 읽기만 한다.

| 구성                               | 찾는 방법                                                                                  | 처리                       |
| ---------------------------------- | ------------------------------------------------------------------------------------------ | -------------------------- |
| 중첩 namespace (Edge 등 Chromium)  | 프로세스의 `/proc/PID/ns/pid`·`ns/user`가 컨테이너 init과 다름                             | 거부, 프로세스 이름 표시   |
| `docker exec`로 띄운 프로세스      | `PPid`를 따라가도 컨테이너 init에 닿지 않음 (`raven run`, `raven shell`, 웹 앱 실행)       | 경고 후 거부 또는 확인     |
| 바깥과 연결된 unix socket          | `ss -xp`에서 세션 프로세스 socket의 상대가 세션 cgroup 밖 (`run --host` 앱, 호스트 중계)    | 거부                       |
| TCP 연결                           | `ss -tnp state established`에서 세션 프로세스                                               | VNC 뷰어는 끊기, 나머지는 거부 |
| tint2                              | 프로세스 이름                                                                              | 끄고 저장, 복원 뒤 다시 띄움 |
| 파일 잠금                          | `/proc/locks`의 PID가 세션 프로세스                                                         | `file-locks`로 저장        |
| 공유 `~/Downloads`·홈의 열린 파일 | `/proc/PID/fd` 링크                                                                         | 크기·mtime·sha256 기록     |
| 복원 때 화면 번호·포트             | `ss -Hltn sport = :PORT`, `ss -Hxl`의 `@/tmp/.X11-unix/XN`                                  | 점유 중이면 복원하지 않음  |

## 보안과 보관

checkpoint에는 세션의 모든 프로세스 메모리가 평문으로 들어간다.

- X 인증 cookie(Xvnc, 모든 X 클라이언트), VNC 비밀번호를 읽은 버퍼, D-Bus 메시지.
- 터미널 scrollback과 입력 중인 명령(비밀번호를 친 경우 포함), 클립보드, kime 입력 버퍼.
- 브라우저의 cookie·세션 token·폼 입력·메모리의 저장 비밀번호(`--password-store=basic`).
- `/dev/shm` 등 tmpfs 내용 전체(tar로 저장), 지운 채 열려 있던 파일의 내용, 환경 변수.

제안:

- 위치: Docker 기본 위치(`/var/lib/docker/containers/<id>/checkpoints/<name>`)를 쓴다. 디렉터리는 root `0700`이고, Docker가 다른 위치에서의 복원을 지원하지 않는다. NFS 홈, 세션 홈 volume, `~/Downloads`에는 두지 않는다. `raven backup`에 넣지 않는다.
- 권한: root만 읽는다. Raven CLI와 웹은 이름·시각·크기·이미지 id만 보여 주고 내용을 읽거나 내보내지 않는다. 웹 컨테이너에는 계속 Docker socket을 주지 않는다.
- 암호화: CRIU에는 이미지 암호화가 없다(`criu check --feature encryption`은 page server 전송용 GnuTLS 지원 여부다). 저장 뒤 세션별 키로 암호화하고 복원 직전에 풀 수 있지만 복원 동안은 평문이 디스크에 있고 키 보관 문제가 남는다. `/var/lib/docker`가 있는 디스크의 암호화(LUKS) 여부를 운영 조건으로 둔다.
- 보존: 세션당 하나만 둔다. 복원에 성공하면 바로 지운다(복원 뒤 파일이 바뀌므로 다시 쓰면 일관성이 깨진다). `kill`, `recreate`, 이미지 교체 때 지운다. 기한(예: 14일)을 넘기면 알리고 지운다.
- 일관성: 저장할 때 열린 파일 목록과 크기·mtime·sha256, 홈 volume과 이미지 id, 화면 번호·포트를 함께 기록하고 복원 전에 비교한다. 다르면 복원하지 않는다.

## M12 판정

**보류.** 근거:

1. Edge를 저장할 수 없다. `edge`는 기본 addon이고 열려 있으면 세션 메모리의 대부분을 차지한다(빈 데스크톱 약 100 MiB, Edge 하나를 연 세션 337 MiB). RAM을 회수할 가치가 큰 세션이 바로 저장되지 않는 세션이다. CRIU 4.3.1은 중첩 PID·user namespace를 거부하고 이를 피하는 옵션이 없으며, sandbox를 끄는 `--no-sandbox`는 Raven에서 쓰지 않는다.
2. 운영 서버의 공유 설정을 바꿔야 한다. CRIU는 Ubuntu archive 밖의 패키지이고, Docker experimental은 모든 사용자가 쓰는 dockerd를 재시작해야 켜지며, CRIU 설정은 서버 전체에 적용된다. "호스트에 패키지를 설치하지 않는다"는 규칙과 충돌하므로 서버 관리자의 결정이 먼저다.
3. Docker checkpoint는 experimental이고 이 버전의 복원 경로에 결함이 있다: 자체 network namespace 컨테이너는 복원되지 않고, 다른 디렉터리의 checkpoint로는 복원할 수 없으며, CRIU 기본 mount 엔진이 named volume에서 실패한다.
4. 지원되는 구성도 Raven 쪽 변경이 많다: tint2 중지, `raven run` 실행 경로, 재시작 정책, 뷰어와 `run --host` 연결, 파일 일관성 기록, checkpoint가 있는 세션의 명령 제한.

다음이 모두 갖춰지면 조건부로 착수할 수 있다.

- 서버 관리자가 CRIU 설치와 Docker experimental을 승인하거나, checkpoint를 쓸 세션만 따로 두는 호스트를 마련한다.
- Edge 문제를 다음 중 하나로 정한다: CRIU가 중첩 PID·user namespace를 지원한다, 또는 Edge 같은 중첩 namespace 프로세스가 있는 세션은 저장하지 않는다는 범위에 합의한다(저장 전에 [탐지](#저장-전-탐지와-거부)해 거부하고 사용자가 Edge를 닫고 저장).
- 그때의 지원 범위는 3단계 구성(Xvnc, xmonad·openbox, D-Bus, xterm·bash, PCManFM, kime, 메뉴로 띄운 앱)이고, CRIU 옵션은 dump에 `file-locks`, restore에 `file-locks`와 `mntns-compat-mode`다. `tcp-established`와 `ext-unix-sk`는 쓰지 않는다.

## 확인하지 않은 것

- 운영 서버 자체에서의 동작. lab은 같은 패키지와 커널이지만 dockerd가 컨테이너 안에 있고, cgroup 드라이버(cgroupfs)와 `/var/lib/docker`의 mount 공유 관계가 다르다. mount-v2 실패가 운영 구조에서도 나는지 모른다.
- 실제 커널 재부팅. lab 재시작은 프로세스와 `/run`만 지운다. 다른 커널 버전으로 복원하는 경우도 시험하지 않았다.
- systemd cgroup 드라이버와 CRIU의 cgroup 복원.
- 운영 host network namespace의 uevent·route netlink 부하에서의 성공률. Edge(`--no-sandbox` 참고 실험)의 udev·route netlink socket은 서버 udevd 이벤트를 받는다.
- CRIU PPA 패키지(lab은 소스 빌드). LZ4 압축(`--compress`)으로 줄어드는 크기와 시간.
- 수 GB 메모리 세션, 큰 `/dev/shm`, 동시에 여러 세션 저장. 저장 중 디스크 부족과 Ctrl-C.
- noVNC 웹 화면으로 다시 연결(웹 중계와 같은 RFB 인증과 화면 갱신까지만 확인).
- `raven attach`의 X11 뷰어(세션 안 프로세스라 저장 전에 끊는 대상).
