# noVNC 브라우저 검사

- `browser.js`는 [noVNC v1.7.0의 원본](https://github.com/novnc/noVNC/blob/v1.7.0/core/util/browser.js)에 대한 로컬 수정본이다. 원본의 MPL 2.0 표기를 유지한다. Docker 이미지에는 noVNC의 `LICENSE.txt`도 함께 배포한다.
- H.264 지원 검사는 API 누락·예외·2초 시간 초과에서 false를 반환한다. 생성한 decoder는 성공·실패 모두 종료하며, 시간 초과 뒤 늦게 도착한 지원 응답으로 decoder를 새로 만들지 않는다.
- Android Chromium의 완료되지 않는 `flush()`는 [이슈 #2034](https://github.com/novnc/noVNC/issues/2034), 일부 구현에서 누락된 `isConfigSupported()`는 [이슈 #2060](https://github.com/novnc/noVNC/issues/2060)을 참고한다. 이 검사 결과는 선택적 영상 인코딩 지원 여부이며 Raven 세션 목록을 불러오는 조건이 아니다.
- Raven은 화면 연결 시에만 RFB를 import하고, module graph 로딩에 10초 상한을 둔다. 기능 검사 결과는 import가 완료될 때 확정되므로 첫 연결이 검사와 경쟁하지 않는다.
- 배포 경로는 `/static/novnc-1.7.0-raven1/`이다. 호환성 수정본을 바꿀 때 경로 revision도 바꿔 모바일 브라우저가 이전 module graph를 cache에서 재사용하지 않게 한다.
- noVNC 버전을 올릴 때 원본 변경과 이 파일을 대조하고 `tests/web_module_loading.py`의 정상 지원·실패·시간 초과·decoder 정리 검사를 실행한다.
