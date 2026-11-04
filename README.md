# 전화한눈 웹

전화번호·기관명 부분검색, 업종 필터, 출처·기준일 표시와 데이터 상태 화면을 제공합니다.

## 자동화

매주 월요일 06:23 KST 및 main 변경 시 공식 원본 수집 → 품질 검사 → 승인 번호만 JSON/SQLite 출력 → GitHub Pages 배포. 실패 시 기존 배포를 유지합니다. 원본과 후보·검토 데이터는 공개 배포하지 않습니다.

웹 검색은 브라우저 안에서 처리하며 검색어를 서버로 보내지 않습니다. Android는 같은 공개 DB를 HTTPS로 다운로드하고 SHA-256·SQLite 무결성·스키마 확인 후 교체합니다. 다운로드 DB의 신뢰는 HTTPS 배포 경로에 기반하며 APK 내장 DB의 서명 검증과는 별개입니다.

수집 주기·마지막 성공 수집·원본별 세부사항은 data-status.html에서 확인합니다. 실패 시 Actions 기록을 확인합니다.

실행: `python -m unittest discover -s tests -v`, `python scripts/collect.py`, `python scripts/export_web.py`.
