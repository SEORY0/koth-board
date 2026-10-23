# koth-board

킹오브힐 후보 파일을 모으고, 점수순으로 보고, 리드가 채택·기각하는 팀 내부 보드.

## 실행

```bash
cp .env.example .env   # TEAM_PW, LEAD_PW, SECRET 채우기
cp deploy/koth*.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now koth koth-tunnel
curl -s 127.0.0.1:8788/quicktunnel   # 외부 주소 (터널 재시작 시 바뀜)
```

- 팀원: 팀 비번으로 로그인 → 문제 → 후보 제출
- 리드: 리드 비번으로 로그인 → 문제 추가, 검증 시작·재채점·채택·기각
- 문제 설정에 디스코드 포럼 웹훅을 넣으면 제출마다 스레드가 생기고 결과가 답글로 달림
- 로그: `journalctl --user -u koth -f`
