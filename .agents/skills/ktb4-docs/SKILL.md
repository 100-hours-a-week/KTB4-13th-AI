---
name: ktb4-docs
description: KTB4-13th-wiki 팀 문서 스킬. 레포 docs/ 에서 문서를 새로 쓰거나, GitHub Wiki 원본(backup/)을 팀 규칙으로 변환하거나, 문서 규칙·원본 보존을 점검하거나, docs를 위키에 반영(사이드바 생성 포함)할 때 반드시 사용한다. 설계·선택 근거·API/테이블 명세·결정 로그·기획·가이드 문서, "위키 정리", "문서 변환", "문서 점검" 요청이 모두 해당한다.
---

# KTB4 문서 스킬

원본 위키는 그대로 두고, 팀 규칙을 적용한 문서를 레포 `docs/`에 만든 뒤 위키에 **새 페이지로** 반영한다. 변환은 **구조(목차·소제목)와 내용(문장의 정보)은 그대로, 형식(문체·표기·블록 모양)만 세 파트 공통으로** 맞추는 작업이다.

```text
원본 위키 ──백업(읽기 전용)──▶ backup/wiki-original-YYYY-MM-DD/
                                   │ 변환 (1 원본 = 1 문서, 구조·내용 그대로, 형식 통일)
                                   ▼
                                 docs/  ◀── 수정은 항상 여기 (PR)
                                   │ wiki_export.py (main 머지 시 CI 자동, 결정 #14)
                                   ▼
                          위키 새 페이지 + _Sidebar
```

## 절대 규칙 — 원본 보호

1. `backup/` 은 **읽기만** 한다. 파일 수정·삭제·이동·재생성 금지.
2. 원본 위키 페이지를 수정하지 않는다. 위키 반영은 `scripts/wiki_export.py` 로만 한다 (`main` 머지 시 CI가 실행, 결정 #14).
3. 원본 위키와 같은 이름의 페이지를 만들지 않는다. 예외는 `_Sidebar.md` 하나(백업 확인 후).
4. 아무것도 삭제하지 않는다. docs에서 문서를 빼도 위키 페이지는 남긴다.
5. 변환은 **구조와 내용을 그대로 두고 형식만 통일**한다. 섹션을 옮기거나 합치거나 나누지 않고, 문장을 요약·축약하지 않는다. 상한을 넘어도 부록으로 보내지 않고 제자리에서 접는다.
6. 판단이 필요한 것(내용 충돌, 소속, 중복, 뜻이 애매한 문장)은 스스로 정하지 않고 사용자에게 묻는다.

이 규칙과 사용자 요청이 부딪히면 규칙을 설명하고 멈춘다.

## 요청별 작업

| 요청 | 모드 | 먼저 읽을 파일 |
|---|---|---|
| 새 문서 작성 | 작성 | `references/decisions.md`, `references/naming-and-links.md`, 해당 템플릿, `references/style-guide.md`, `references/block-patterns.md` |
| 위키 원본을 규칙대로 변환 | 변환 | `references/conversion-rules.md`, `references/block-patterns.md`, `references/style-guide.md`, `references/page-map.md` |
| 문서 점검 | 점검 | `references/style-guide.md`, `references/block-patterns.md` |
| 위키에 반영, 사이드바 | 반영 | `references/naming-and-links.md` |
| 원본 위키가 바뀌었는지 확인 | 추적 | `references/page-map.md` |
| 코드와 명세 비교 | 동기화 | 해당 명세 문서 |

템플릿: `assets/templates/` — hub, design, rationale, spec, decision(로그·ADR), planning, guide, appendix. **새 문서 전용.** 변환 문서는 템플릿의 섹션 구성을 쓰지 않고 frontmatter + `**요약**` 한 줄만 가져온다.

## 작성 모드

1. 문서 종류와 위치를 정한다. 제목·폴더·frontmatter는 `naming-and-links.md` 를 따른다.
2. **목차를 먼저 보여주고 승인받는다.** 승인 전에는 본문을 쓰지 않는다.
3. 템플릿대로 쓴다. 새 문장은 "~한다"체.
4. 다른 파트가 알아야 하는 결정은 `docs/dec/000-decision-log.md` 에 한 줄 추가한다.
5. `python scripts/check_docs.py docs` 를 실행하고 오류를 0으로 만든다.
6. `docs/README.md` 목차 표의 해당 묶음 행에 이번에 만든 문서 링크를 추가한다(`(변환 후 추가)` 자리표시자면 지우고 채운다).

## 변환 모드 (가장 조심)

`references/conversion-rules.md` 와 `references/block-patterns.md` 를 반드시 읽고 따른다. 요약:

**1 원본 페이지 = 1 문서.** 목차·소제목·문단 소속은 원본 그대로, 문장의 정보도 그대로. 바꾸는 건 형식뿐이다 — 문체 어미, 표기(펜스·구분선·인라인 코드·표 기호), 블록 모양(반복 항목 `<details>` 1단, 레이블 이름·순서, 레이블 불릿, 긴 셀 각주, 다이어그램 노드 표, 긴 코드 제자리 접기, 참고 링크 모으기). 유일한 구조 손질은 "굵은 줄·바깥 `<details>`로 쓴 제목을 진짜 `###` 제목으로" (글자·위치 그대로).

**중간에 멈춰 승인받지 않는다.** 요청 한 번에 끝까지 진행하고 결과를 보고한다 (결정 #15). 멈추는 경우는 "원본끼리 충돌인데 `decisions.md` 에 기준이 없을 때"뿐이다.

1. **원본은 `backup/` 에서만 읽는다.** 라이브 위키를 원본으로 쓰지 않는다.
2. `page-map.md` 에서 원본 파일과 새 문서 제목·폴더를 확인한다. 상태가 `보류`면 멈춘다.
3. 새 파일: frontmatter + `**요약**` 한 줄 + 원문 전체. 제목 트리는 원본 그대로, 최상위만 `##`. 첫 줄이 페이지 제목 반복이면 본문에서 뺀다.
4. 1단계 표기 → 2단계 블록 모양을 적용한다. 3단계(문장 손질)는 파트가 요청했을 때만.
5. 원본끼리 내용이 다르면 둘 다 그대로 두고, 기준값이 아닌 문장 아래 `> 기준: …` 한 줄을 추가한다. **기준이 없을 때만** 멈추고 묻는다.
6. 검사를 실행한다.
   ```bash
   python scripts/preserve_check.py --docs docs --backup-root backup --group <group>
   python scripts/check_docs.py docs
   ```
7. `제목 트리`, `누락 의심`, `뜻 바뀜 의심`, `숫자 누락` 이 0이 될 때까지 고친다. **원본 문장을 지워서 통과시키지 않는다.** 3단계를 켜지 않았는데 `문장 변경` 경고가 나오면 되돌린다 (`문체 변경`은 정보로만 집계된다).
7-1. `check_docs.py`의 `긴 블록을 제자리에서 접기`, `가짜 중첩 목록` 경고도 확인한다. 둘 다 4단계(2단계 블록 모양)를 실제로 입혔는지 기계가 잡아내는 신호다 — 오류가 아니라서 통과는 되지만, **각 경고를 block-patterns.md 모양으로 고치거나, 못 고칠 사정이 있으면 9단계 보고에 남긴다.** 그냥 두고 보고도 안 하는 건 안 된다.
8. `page-map.md` 상태를 `변환됨(확인 대기)` 로 바꾸고, `docs/README.md` 목차 표의 해당 묶음 행에 이번에 만든 문서 링크를 추가한다(`(변환 후 추가)` 자리표시자면 지우고 채운다). PR을 만든다.
9. 검사 결과, 적용한 블록 패턴, 승격한 제목 목록, `문장 변경` 목록(있다면 원본·변환본 나란히), 카탈로그에 없던 블록, **남겨둔 `check_docs.py` 경고와 그 이유**를 PR과 함께 한 번에 보고한다. 여기서부터는 사후 확인 — 문제 있으면 사용자가 다시 요청.

## 점검 모드

`check_docs.py` 와 (변환 문서면) `preserve_check.py` 를 실행해 결과를 표로 보고한다. 기계로 못 잡는 것만 추가로 본다: 요약이 내용과 맞는지, 결정 로그 누락, 다른 파트 요구사항이 본문에 묻혀 있는지. 원본 보호 규칙 때문에 **점검 중 파일을 고치지 않는다.** 고칠지 먼저 묻는다.

## 반영 모드 (2026-09-17부터 자동)

`main`에 `docs/**`가 머지되면 `.github/workflows/wiki-sync.yml`이 자동으로 `wiki_export.py --sidebar --apply`를 실행하고 위키에 커밋·푸시한다. **사람이 다시 실행할 필요 없다.** 이 워크플로에 문제가 있는지(시크릿 누락, 실행 실패)는 GitHub Actions 탭에서 확인한다.

스크립트 자체의 안전장치(원본과 이름 겹침, 자기가 안 만든 페이지 덮어쓰기, 삭제 금지)는 그대로 있다 — 자동화된 건 "누가 명령어를 치느냐"이지 "무엇을 덮어써도 되느냐"가 아니다.

수동으로 다시 돌리고 싶을 때(디버깅 등)는 예전 방식도 쓸 수 있다:
```bash
git clone https://github.com/100-hours-a-week/KTB4-13th-wiki.wiki.git ../wiki-publish
python scripts/wiki_export.py --docs docs --backup-root backup --wiki ../wiki-publish            # 계획만 출력
python scripts/wiki_export.py --docs docs --backup-root backup --wiki ../wiki-publish --sidebar --apply
git -C ../wiki-publish push  # 위 계획이 맞으면 직접
```

## 추적 모드

```bash
bash scripts/wiki_changed_since_backup.sh backup/wiki-original-YYYY-MM-DD
```

백업 이후 바뀐 원본 페이지 목록이 나온다. 해당 행을 `page-map.md` 에서 찾아 새 백업 → 재변환 대상으로 표시한다. 기존 백업 폴더는 고치지 않고 **새 날짜 폴더**를 만든다 (`scripts/backup_wiki.sh`).

## 동기화 모드

코드와 명세를 비교해 차이만 표로 보고한다. 어느 쪽이 맞는지 판단하지 않는다. 명세를 고칠지 코드를 고칠지는 담당 파트가 정한다.

## 스크립트

| 스크립트 | 역할 |
|---|---|
| `scripts/backup_wiki.sh` | 위키 원본을 `backup/wiki-original-날짜/` 로 복사, 해시 목록 생성 |
| `scripts/verify_backup.py` | 백업이 바뀌지 않았는지 해시로 확인 |
| `scripts/preserve_check.py` | 원본 대비 제목 트리·누락·숫자·뜻 바뀜 검사 (문체 어미 차이는 무시) |
| `scripts/check_docs.py` | 이름·frontmatter·분량·제목·문체·코드 블록·링크·자리표시자·가짜 중첩 목록 검사 (변환 문서는 `####`·분량이 경고) |
| `scripts/wiki_export.py` | docs → 위키 새 페이지·사이드바 생성 (덮어쓰기 방지) |
| `scripts/wiki_changed_since_backup.sh` | 백업 이후 원본 위키 변경 목록 |
