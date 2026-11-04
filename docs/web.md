# 브라우저 접속

Raven 전용 단일 계정으로 로그인해 데스크톱 세션을 만들고 관리한다. noVNC가 화면과 키보드·마우스를 중계하므로 클라이언트에 X 서버가 필요 없다. 서버 SSH 로그인과 웹 계정은 별개이며 sshd 설정을 바꾸지 않는다.

추가 기능의 구현 순서와 완료 조건은 [개발 마일스톤](./roadmap.md)을 따른다.

```text
Browser ── HTTPS / WSS ── Web container ── private Unix socket ── Host broker ── Xvnc
```

## SSH 터널로 시작

서버에 기존 Raven 설치와 세션 이미지가 있어야 한다. 웹 이미지와 계정을 준비한다. 웹 비밀번호는 터미널에서 두 번 입력하며 명령 인자나 환경 변수로 받지 않는다.

```bash
raven web build
raven web init --username junho --origin https://localhost:8443
raven web certificate
raven web start
raven web status
```

- 비밀번호는 8자 이상이며 흔한 비밀번호, 숫자만 있는 값, 사용자 이름과 유사한 값은 거부한다.
- 계정은 하나만 만들 수 있다. 공개 회원가입과 관리자 웹 화면은 없다.
- 초기화할 때 해당 hostname의 self-signed TLS 인증서를 생성한다. 기본 유효 기간은 365일이다. `raven web certificate`는 공개 인증서만 출력한다.
- 처음 로그인하기 전에 SSH로 확인한 인증서의 SHA-256 fingerprint와 브라우저가 표시하는 인증서를 대조하고 명시적으로 신뢰한다. 인증서가 예상과 다르면 비밀번호를 입력하지 않는다. Windows에서의 등록 절차는 [인증서 신뢰 설정](#windows에서-인증서-신뢰)을 따른다.
- origin은 브라우저에서 사용할 주소 전체다. 기본 설정에서는 `https://localhost:8443`을 사용한다. `127.0.0.1`, 다른 hostname 또는 다른 포트는 같은 주소로 취급하지 않는다.

클라이언트에서 터널을 열고 브라우저의 `https://localhost:8443`으로 접속한다.

```bash
ssh -N -L 8443:127.0.0.1:8443 junho@server
```

다른 SSH 사용자가 같은 포트로 터널을 열더라도 Raven 웹 로그인은 별도로 필요하다. 터널이 끊기거나 브라우저를 닫아도 데스크톱 세션은 유지된다.

## Windows에서 인증서 신뢰

접속 PC에는 공개 인증서만 가져와 현재 사용자의 신뢰 저장소에 등록한다. 아래는 Windows와 Chrome 기준이며 `user@server`는 실제 SSH 계정과 서버 주소로 바꾼다. 개인 키인 `/data/tls.key`는 서버에 둔다.

1. 신뢰하는 SSH 연결의 서버 셸에서 현재 인증서의 SHA-256 fingerprint를 확인한다. 인증서를 교체하면 값이 바뀌므로 이전에 복사해 둔 값 대신 현재 값을 확인한다.

   ```bash
   raven web certificate | openssl x509 -noout -fingerprint -sha256
   ```

2. 접속 PC의 PowerShell에서 공개 인증서를 가져온다. ASCII로 저장해 PowerShell 버전에 따른 텍스트 인코딩 차이를 피한다.

   ```powershell
   ssh user@server '~/.local/bin/raven web certificate' |
       Set-Content -Encoding ascii .\raven-localhost.crt
   ```

3. DER 형식으로 변환하고 SHA-256을 계산한다. PEM 텍스트 파일 자체의 hash는 인증서 fingerprint와 다르므로 변환한 `.cer` 파일을 사용한다.

   ```powershell
   certutil -decode .\raven-localhost.crt .\raven-localhost.cer
   certutil -hashfile .\raven-localhost.cer SHA256
   ```

4. 서버에서 확인한 fingerprint와 비교한다. 콜론·공백·대소문자를 제외하고 값이 일치할 때만 현재 Windows 사용자의 신뢰 저장소에 등록한다.

   ```powershell
   certutil -user -addstore Root .\raven-localhost.cer
   ```

5. Chrome을 완전히 종료했다가 다시 열고 SSH 터널을 유지한 상태에서 `https://localhost:8443`으로 접속한다. `localhost`용 인증서는 서버 IP로 접속할 때의 주소 불일치를 해결하지 않는다.

`-user`는 PC 전체가 아닌 현재 사용자의 인증서 저장소를 선택한다. Chrome은 이 저장소에 명시적으로 등록한 인증서를 신뢰 판단에 사용한다. [Microsoft certutil 설명](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/certutil#-addstore), [Chrome 신뢰 저장소 설명](https://chromium.googlesource.com/chromium/src/+/main/net/data/ssl/chrome_root_store/faq.md#how-does-the-chrome-certificate-verifier-integrate-with-platform-trust-stores-for-local-trust-decisions)을 참고한다.

## 접속 설정 저장

`configure`는 다음 시작에 사용할 설정을 저장한다. 아래는 서버 포트를 9444로 바꾸고 브라우저 주소는 `https://localhost:8443`으로 유지하는 예다. `junho@server`는 접속 PC에서 사용하는 SSH 계정과 서버 주소 또는 SSH alias로 바꾼다.

```bash
raven web configure --listen 127.0.0.1 --port 9444 --ssh-target junho@server
raven web stop
raven web start
raven web status
```

`status`에 표시된 터널 명령을 접속 PC에서 실행한다.

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8443:127.0.0.1:9444 junho@server
```

- `--listen`: 서버에서 HTTPS를 받을 IP 주소다. 신규 설치의 기본값은 `127.0.0.1`이다.
- `--port`: 서버 포트다. 컨테이너 내부 TLS 포트는 8443으로 고정이며 브라우저 포트와 다를 수 있다. 범위는 1024–65535다.
- `--origin`: 브라우저 주소다. scheme·hostname·포트를 정확히 맞춘다. 경로와 끝의 `/`는 넣지 않는다.
- `--ssh-target`, `--ssh-port`: 터널 명령 출력에 사용하는 SSH 접속 대상과 포트다. sshd나 SSH 계정 설정을 변경하지 않는다. SSH 포트를 생략하면 접속 PC의 SSH 설정을 따른다.
- 지정하지 않은 항목은 저장된 값을 유지한다. 처음 설정을 저장하는 기존 설치는 남아 있는 웹 컨테이너의 포트 매핑을 읽으므로 9444 같은 기존 포트가 기본값으로 바뀌지 않는다.
- `configure`는 실행 중인 게이트웨이를 바꾸지 않는다. `status`에서 현재 설정과 다음 시작 설정을 확인한 뒤 `stop`, `start`로 적용한다. Docker의 자동 재시작도 실행 중 컨테이너의 origin을 유지한다.
- M1 이전 방식의 게이트웨이가 실행 중이면 origin 변경 전에 한 번 `stop`해야 한다. `stop → configure --origin ... → start` 또는 먼저 `stop → start`로 이전한다. 서버 포트와 SSH 안내 값만 저장하는 작업은 실행 중에도 가능하다.
- `start --port 9444`처럼 시작 명령에서 값을 지정할 수도 있다. 포트와 origin을 검사하고 HTTPS 응답·인증서 검증이 성공한 뒤 저장한다. 이후에는 옵션 없는 `start`로 같은 설정을 사용한다. 실행 중인 게이트웨이와 다른 값으로 시작하려 하면 먼저 중지하도록 안내한다.
- 포트 충돌·SAN 불일치·기동 실패는 기존 설정을 덮어쓰지 않는다. 새 게이트웨이 기동이 실패하면 이전 중지 컨테이너를 복원하고, 실패한 컨테이너는 출력된 `-failed-<id>` 이름으로 남겨 로그를 확인하게 한다.
- 변경 전 `/data/config.json.bak.configure-v1.<timestamp>-<suffix>`에 원본을 `0600`으로 백업하고 내용을 확인한 뒤 원자적으로 교체한다. 값이 같으면 백업과 파일 교체를 생략한다. 계정·인증서·데스크톱 데이터는 설정 변경 대상에 포함되지 않는다.

접속 PC의 8443 포트가 사용 중이면 브라우저 origin도 변경한다. 다음 예는 서버 9444와 접속 PC 10443을 사용한다.

```bash
raven web stop
raven web configure --port 9444 --origin https://localhost:10443
raven web start
raven web status
```

터널은 `127.0.0.1:10443:127.0.0.1:9444`를 연결하고 브라우저는 `https://localhost:10443`으로 연다. 포트만 바꾸면 기존 localhost 인증서를 계속 사용할 수 있다. hostname/IP는 현재 인증서의 SAN과 정확히 일치해야 하며 wildcard SAN은 지원하지 않는다. 인증서 교체는 M8의 범위다. `:08443`, 확장 IPv6, percent escape처럼 브라우저가 다른 주소로 정규화하는 표기는 거부한다. 기본 HTTPS 포트는 `:443`을 생략한다.

## 상태 확인

`raven web status`는 다음을 표시한다.

- 게이트웨이 상태와 Docker 시작 오류·종료 코드, 호스트 중계의 실행 여부.
- 실행 중인 실제 listen 주소·서버 포트와 브라우저 URL, 다음 시작에 사용할 저장 설정.
- 서버 hostname, 명시적으로 저장한 SSH 접속 대상, 접속 PC에서 실행할 터널 명령.
- 실행 이미지와 image ID, 데이터 volume, 게이트웨이·중계 로그 위치.

서버 hostname은 접속 PC에서 도달 가능한 SSH 주소라고 보장하지 않는다. SSH 대상이 없으면 `USER@HOST` 자리 표시자와 `configure --ssh-target` 안내를 출력한다. 직접 HTTPS 접속용 origin에는 SSH 터널 대신 직접 접속 안내를 표시한다.

`status`는 서비스·중계를 시작하거나 설정·runtime 파일을 만들지 않는다. 기존 데이터 volume은 네트워크가 차단된 일회성 컨테이너에서 읽기 전용으로 조회한다. 다른 로그인 셸에서 호출해도 기존 게이트웨이에 연결된 runtime 경로를 사용한다. 초기화 전, Docker 접근 실패, 중계 종료, 설정 파일 읽기 실패를 구분한다. HTTPS 응답 등 상세 진단을 수행하는 `raven web doctor`는 M2에서 추가한다.

## 화면 사용

- 로그인 후 세션의 연결 버튼을 누른다. 중지된 세션은 기존 CLI의 접속 절차로 시작한다.
- 화면 크기 맞춤을 켜면 데스크톱 해상도가 브라우저 영역을 따라 바뀐다. 같은 세션을 보는 다른 뷰어에도 반영된다.
- 전체 화면, Ctrl + Alt + Del, 연결 종료 후 재연결을 지원한다.
- 세션 목록으로 돌아가면 해당 화면 연결만 끊는다. 로그아웃하면 같은 로그인 세션으로 연 다른 탭의 화면 연결도 닫는다.
- 새 세션에서 이름, 창 관리자, 초기 해상도, 시작 명령을 정한다. 생성이 끝나면 화면에 연결한다. 이름은 영문자로 시작하는 64자 이하의 영문·숫자·`_`·`.`·`-` 조합이다.
- 이름 변경은 실행 중인 앱에 영향을 주지 않는다. 명령 실행은 선택한 세션 컨테이너 안에서 shell 명령을 시작한다. 명령은 4096자 이하이며 호스트 직접 실행 옵션은 제공하지 않는다.
- 삭제는 세션 이름을 직접 입력하고 영구 삭제 버튼을 눌러 확인한다. 세션과 홈 volume을 삭제하며 복구할 수 없다. 호스트의 Downloads는 기존 CLI와 같이 남는다. 이름 변경·명령 실행·삭제는 고유 컨테이너 ID도 검사하므로, 세션 번호가 재사용된 오래된 화면의 요청은 거부한다.
- 기존 CLI도 계속 사용할 수 있다.

```bash
raven new -d -s desktop
raven rename desktop work
raven run -t work xterm
```

## 직접 HTTPS 접속

SSH 터널 없이 접속하려면 최초 초기화에서 실제 hostname과 포트를 정하고, 서버가 그 주소의 HTTPS를 제공하도록 실행한다. 정상적인 인증서 검증이 가능한 TLS 인증서와 네트워크 접근 정책은 운영 환경에 맞게 준비한다. 기본 인증서는 self-signed이며 공인 인증서 자동 발급·갱신 기능은 제공하지 않는다.

```bash
raven web init --username junho --origin https://raven.example.com:8443
raven web start --listen 0.0.0.0 --port 8443
```

- 신규 설치의 기본 listen 주소는 `127.0.0.1`이다. 이후에는 저장된 주소를 사용한다. 전체 인터페이스에 열려면 위처럼 명시한다.
- 초기화한 origin은 재초기화로 덮어쓰지 않는다. TLS 인증서와 origin 설정은 데이터 volume에 보관한다.
- 웹 컨테이너는 TLS를 직접 처리하며 외부의 `X-Forwarded-*` 헤더를 신뢰하지 않는다.

## 운영

```bash
raven web status
raven web password --username junho
raven web stop
raven web start
```

- 비밀번호를 바꾸면 기존 웹 로그인은 모두 무효화되고 열린 화면은 2초 이내에 닫힌다.
- `stop`은 웹 컨테이너와 웹 중계만 중지한다. 데스크톱 세션과 기존 호스트 앱 중계는 계속 실행된다.
- SSH 로그인 셸을 닫아도 웹 중계는 실행된다. 서버를 재부팅하면 `raven web start`로 호스트 중계를 다시 시작해야 한다.
- `status`의 현재 주소와 다음 시작 주소가 다르면 변경 적용이 대기 중이다. 컨테이너가 실행 중이어도 중계가 중지되어 있으면 세션 목록을 가져올 수 없다.
- 웹 이미지를 갱신할 때는 `build`, `stop`, `start` 순서로 실행한다. 계정과 인증서는 유지된다.

## 자원 제한

- 웹 게이트웨이와 설정·계정 도우미 컨테이너의 메모리 상한은 1GiB, CPU 상한은 1개다. 게이트웨이의 process/thread 상한은 256개다.
- `start`는 새 게이트웨이를 제한과 함께 만들고, 이미 실행 중인 게이트웨이에도 메모리·CPU 제한을 적용한다. 이미지 재빌드는 필요하지 않다.
- `--memory-swap`을 `--memory`와 같게 설정하므로 컨테이너가 swap을 사용하지 못한다. [Docker 자원 제한](https://docs.docker.com/engine/containers/resource_constraints/#--memory-swap-details)의 의미를 따른다.
- 메모리 상한은 동시에 수행되는 비밀번호 검증에 필요한 여유를 포함한다. 상한을 초과한 프로세스는 컨테이너 안에서 OOM 종료될 수 있다.
- 데스크톱 컨테이너는 각각 메모리 2GiB·CPU 2개·process/thread 512개로 별도 제한한다. 기존 데스크톱에 적용하는 방법은 [세션 재시작과 보존](./usage.md#재시작과-보존)을 따른다.
- 이 제한은 컨테이너별 상한이다. 호스트 중계, `run --host`로 실행한 앱, Docker daemon과 다른 프로젝트의 컨테이너에는 적용하지 않는다. 세션 수가 늘면 전체 메모리 허용량도 늘어난다.

현재 사용량과 상한은 `docker stats --no-stream raven-USER-web`으로 확인한다. `USER`는 `raven web status`에 표시되는 실제 컨테이너 이름에 맞춘다. 누수 여부는 시간에 따른 사용량과 OOM 기록으로 판단한다. 재부팅 직후 메모리가 비어 있거나 컨테이너의 `OOMKilled`가 false라는 사실만으로 이전 호스트 장애의 원인을 배제하지 않는다.

## 접근 경계

- 웹 로그인은 Argon2id password hash를 사용한다. 전체 서비스에서 로그인 시도 5회마다 마지막 시도부터 1분 동안 추가 시도를 차단한다. 차단 응답은 남은 대기 시간을 알려 주며, 대기 중 재요청해도 차단 시간을 연장하지 않는다.
- 로그인 cookie는 `Secure`, `HttpOnly`, `SameSite=Strict`다. 로그인은 8시간 뒤 만료되며 HTTP API와 WebSocket에 모두 적용된다.
- 생성·이름 변경·삭제·명령 실행을 포함해 상태를 바꾸는 HTTP 요청에는 로그인과 CSRF 검사를 적용한다. JSON 필드와 입력값을 검증하고, 기존 세션 변경에는 고유 컨테이너 ID의 일치를 요구한다. 삭제 API는 대상 번호의 명시적 확인도 요구한다. WebSocket은 정확한 HTTPS origin, Host, 로그인 세션을 확인한 뒤 중계에 연결한다. 로그아웃하면 이미 열린 연결도 닫는다.
- `Referrer-Policy: same-origin`으로 같은 사이트의 로그인·로그아웃 폼에 출처 정보를 유지한다. 외부 사이트에는 Referer를 보내지 않는다. `no-referrer`는 브라우저 폼의 Origin을 `null`로 만들어 CSRF 검사와 충돌하므로 사용하지 않는다.
- 웹 컨테이너는 호스트 사용자의 UID로 실행하지만 별도 network namespace를 사용한다. Docker socket과 호스트 홈을 mount하지 않고 capability를 제거하며 root filesystem을 읽기 전용으로 둔다.
- 호스트 중계는 소유자 전용 디렉터리 `0700`과 socket `0600`을 사용한다. `SO_PEERCRED`의 UID와 세션의 Docker 소유자 label·컨테이너 이름을 확인한다. 목적지 주소나 포트를 웹 요청에서 받지 않는다.
- 웹의 명령 실행은 확인한 컨테이너 ID를 대상으로 `docker exec`를 호출한다. 입력을 호스트 shell이나 CLI의 `--host`·`@LABEL` 분기로 전달하지 않는다. 세션의 기존 Downloads 공유와 메뉴 권한은 동일하다.
- VNC 연결을 연 뒤 해당 TCP 연결의 inode가 실제 세션 컨테이너의 Xvnc 프로세스에 속하는지 확인한다. 다른 SSH 사용자가 포트를 먼저 차지한 경우 그 연결을 전달하지 않는다.
- 기존 VNC password 인증은 유지한다. 웹 로그인 비밀번호와 별개인 VNC credential은 인증된 HTTPS 응답으로 noVNC에만 전달하고 응답 cache, URL, 브라우저 저장소에 보관하지 않는다.
- 본인의 OS 계정은 기존 CLI를 계속 사용할 수 있다. root, sudo 또는 Docker 관리 권한이 있는 사용자는 앱 인증 밖에서 컨테이너와 데이터를 읽을 수 있다. 웹 로그인은 그 권한을 제한하는 경계가 아니다.

## 서버 자원과 제거

기본 instance 이름은 `web`이다. `RAVEN_WEB_INSTANCE`는 시험 환경을 분리할 때 사용하며, 바꾼 경우 아래 이름의 마지막 `web` 부분도 바뀐다.

- 이미지: `raven-web:latest`. `RAVEN_WEB_IMAGE`로 변경한다. 데스크톱 이미지와 별도로 빌드한다.
- 컨테이너: `raven-<user>-web`. 웹 전용 `raven.web.uid` label을 사용하며 기존 `raven ls`에는 나타나지 않는다.
- 데이터 volume: `raven-<user>-web-data`. `/data`의 `db.sqlite3`, `secret.key`, `config.json`, 설정 변경 전 백업, `tls.crt`, `tls.key`를 보관한다. NFS 홈을 사용하지 않는다.
- 호스트 runtime 디렉터리: `${XDG_RUNTIME_DIR:-/tmp}/raven-web-<uid>-web`. `broker.sock`, `broker.log`, `manager.lock`을 둔다.
- 프로세스: `raven_web_host.py`와 웹 컨테이너. 호스트에 Python 패키지를 설치하지 않는다.

`raven uninstall`은 기존 데스크톱 자원을 제거하며 웹 컨테이너·데이터 volume·웹 이미지는 별도로 남는다. 먼저 `raven web stop`을 실행한다. 웹 데이터를 더 이상 보관하지 않을 때 아래의 `<user>`를 실제 `raven web status`에 나온 이름으로 바꿔 제거한다. 데이터 volume을 지우면 웹 계정과 인증서도 삭제된다.

```bash
docker container rm raven-<user>-web
docker volume rm raven-<user>-web-data
```

## 개발 검증

보안 테스트는 임시 SQLite와 Unix socket을 사용한다. 실제 통합 시험은 별도 계정·volume·컨테이너와 이름 붙인 새 데스크톱을 만들고 시험이 끝나면 해당 Docker 자원만 정리한다. 기존 데스크톱은 열거나 변경하지 않는다.

```bash
python3 -m unittest discover -s tests -p 'test_web_*.py' -v
docker build -f web/Dockerfile --target test -t raven-web:test .
docker run --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m raven-web:test
docker build -f web/Dockerfile -t raven-web:dev .
python3 tests/web_resource_limits.py
python3 tests/web_config_e2e.py
python3 tests/web_e2e.py
```

설정 통합 시험은 기존 형식의 설정 이전, 실제 포트 충돌, 실행·저장 설정 구분, Docker 재시작 시 origin 유지, 기동 실패 복원, 계정·인증서 보존을 확인한다. 실제 SSH 서버 설정이나 사용자 데스크톱은 변경하지 않는다.

자원 시험은 Linux cgroup v2에서 메모리·swap·CPU·process/thread 상한과 동시에 수행하는 비밀번호 검증 5개의 동작을 확인한다. 임시 컨테이너를 시작하기 전에 Docker 설정의 상한부터 검사하므로 제한 없는 컨테이너에서 부하를 발생시키지 않는다.

화면 통합 시험에는 기존 `raven:latest` 세션 이미지가 필요하다. 서로 다른 서버·브라우저 포트를 로컬 TCP 전달기로 연결해 HTTPS 로그인과 WSS/RFB 화면 수신을 확인한다. 이는 실제 접속 PC의 SSH 인증 시험과 구분한다. 웹 서비스 stop/start 후 인증서와 데스크톱 ID가 유지되는지, HTTPS API로 별도 시험 세션을 생성·변경·삭제하고 명령이 시험 컨테이너 안에서 실행되는지 확인한다. 화면 캡처는 출력에 나온 임시 디렉터리에 남긴다. `RAVEN_WEB_BROWSER_IMAGE=headless-browser:1.63.0`을 지정하면 로그인 전 화면과 noVNC JavaScript 로딩도 브라우저에서 검사한다. 브라우저 자동 로그인은 수행하지 않는다.

`python3 tests/web_ui_check.py`는 실제 template·JavaScript·CSS와 별도 API fixture로 관리 화면의 입력, CSRF 전달, 삭제 확인, PC·모바일 배치를 검사한다. 기존 `headless-browser:1.63.0` 이미지를 사용하며 실제 로그인이나 세션 변경은 수행하지 않는다.
