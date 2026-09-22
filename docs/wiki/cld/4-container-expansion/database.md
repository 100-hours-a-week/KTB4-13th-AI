---
wiki: CLD-4 데이터베이스의 한계와 개선
type: design
group: cld-4
owner: 미정
status: 작성중
updated: 2026-09-21
sources:
  - 2.2.-데이터베이스의-한계와-개선.md
order: 4
---
$1## V2 서비스 DB 인프라 도입 판단

### 요약 및 최종 결정

V2는 여러 애플리케이션 인스턴스가 회원·도서·주문·결제 데이터를 함께 사용해야 한다. 따라서 애플리케이션 EC2 내부의 MySQL을 그대로 유지할 수 없으며, **애플리케이션과 DB를 분리해 중앙 데이터 저장소를 구성해야 한다.**

다만 DB를 분리해야 한다는 사실이 곧바로 RDS, Multi-AZ 또는 Read Replica 도입을 의미하지는 않는다. 현재 확인된 비용과 매출 자료를 기준으로 한 결정은 다음과 같다.

| 판단 항목 | 현재 결정 | 재검토 조건 |
| --- | --- | --- |
| 애플리케이션과 DB 분리 | **분리** | V2가 단일 애플리케이션 인스턴스를 계속 사용하고 장시간 중단을 허용하는 경우 |
| DB 운영 방식 | **EC2에서 MySQL 직접 운영** | RDS가 줄이는 순운영비와 EC2 고유 장애·도구 비용의 합이 월 59,400원을 초과하는 경우 |
| DB 토폴로지 | **Standby가 없는 단일 DB 인스턴스** | `RTO 5분·커밋 완료 데이터 RPO 0분`이 필수 정책이거나 이중화의 손실 회피액이 총비용을 초과하는 경우 |
| 읽기 확장 | **Read Replica 미도입** | 쿼리·인덱스 개선 후에도 읽기 병목이 지속되고, 병목으로 인한 손실이 Replica 총비용보다 큰 경우 |
| 상위 사양 | **검토 보류** | 현재 구성으로 해결할 수 없는 요구가 측정 자료로 확인된 경우 |

현재 잠정 기본안은 **별도 EC2에서 운영하는 단일 MySQL 8.4 인스턴스**다. 월 예상 인프라 비용은 약 61,300원이다. 이 선택은 백업·특정 시점 복원·보안 패치·모니터링 절차가 운영 전에 검증된다는 조건에서만 유효하다.

RDS Single-AZ는 같은 비교 사양에서 월 약 120,700원으로, EC2 직접 운영보다 약 59,400원 비싸다. 현재는 RDS가 실제로 줄일 수 있는 운영시간과 EC2 장애 손실을 측정하지 않았다. 따라서 **현재 확보된 자료로 정량화할 수 있는 RDS 편익은 0원으로 두고**, 관련 자료가 확보되면 도입 여부를 재검토한다.

이 문서에서 사용하는 주요 용어는 다음과 같다.

- **단일 DB**: Standby가 없는 단일 MySQL 인스턴스
- **Primary–Standby**: Primary와 다른 가용 영역의 Standby를 이용하는 고가용성 구성
- **관리형 프리미엄**: 같은 토폴로지를 기준으로 RDS가 EC2 직접 운영보다 비싼 금액

### 1. 판단 범위와 서비스 기준

#### 1.1 판단 범위

검토 대상은 회원·도서·주문·결제 데이터를 저장하는 V2 서비스 MySQL이다. AI 전용 데이터 저장소는 이번 판단에서 제외한다.

#### 1.2 서비스 규모

| 항목 | V2 기준 |
| --- | ---: |
| 3년 차 누적 가입자 | 200,000명 |
| MAU | 80,000명 |
| 평균 DAU | 8,000명 |
| 월 예상 매출 | 7,200,000원 |
| 정상 최대 부하 | 50 RPS |
| 검증 최대 부하 | 100 RPS |
| 운영 계획 DB 부하 | 약 200 QPS |
| 검증 계획 DB 부하 | 약 400 QPS |
| 조회 비중 가정 | 83% |

#### 1.3 의사결정 순서

다음 순서에 따라 필요한 최소 구성까지만 검토한다.

1. V2에서 애플리케이션과 DB를 분리해야 하는지 판단한다.
2. DB를 분리한다면 EC2에서 MySQL을 직접 운영할지 RDS를 사용할지 판단한다.
3. 운영 방식을 선택한 뒤 단일 DB로 충분한지 Primary–Standby가 필요한지 판단한다.
4. 선택한 토폴로지의 구현 방식을 결정한다.
5. 실제 읽기 병목이 확인된 경우에만 Read Replica 등 읽기 확장을 검토한다.

비용 대비 효용이 확인되지 않으면 비용이 증가하는 상위 구성에 대한 검토를 종료한다.

### 2. 비용 산정 기준과 판단 원칙

#### 2.1 공통 비교 사양

실제 DB 인스턴스 크기는 3년 누적 데이터와 부하 시험 결과로 결정해야 한다. 현재는 대안 간 비용 차이를 비교하기 위해 다음 사양을 공통 기준으로 사용한다.

- AWS 서울 리전
- EC2 MySQL: `t4g.medium`, 2 vCPU, 메모리 4 GiB
- RDS MySQL: `db.t4g.medium`, 2 vCPU, 메모리 4 GiB
- 데이터 볼륨: gp3 100GB
- EC2 MySQL 백업 비교값: EBS Snapshot 100GB
- On-Demand 730시간/월
- 환율: 1달러당 1,378.04원
- 부가가치세 제외
- 기준 용량을 초과하는 백업·스냅샷·데이터 전송·CPU Credit과 고급 모니터링 비용 제외

단가는 2026년 9월 18일 AWS 공식 Price List를 기준으로 한다. 실제 도입 시점에는 최신 가격을 다시 확인해야 한다.

#### 2.2 월 인프라 비용 요약

| 구성 | 월 예상 비용 | 비고 |
| --- | ---: | --- |
| EC2 MySQL 단일 DB | 약 61,300원 | EC2 1대, gp3 100GB, Snapshot 100GB |
| EC2 MySQL Primary–Standby | 약 115,700원부터 | Witness·Fencing·Proxy·교차 AZ 통신·운영비 제외 |
| RDS MySQL Single-AZ | 약 120,700원 | 관리형 단일 DB |
| RDS MySQL Multi-AZ DB Instance | 약 240,300원 | 관리형 Primary–Standby |
| EC2 읽기 Replica 1대 추가 | 약 54,400원부터 | 데이터 전송·운영비 제외 |
| RDS Read Replica 1대 추가 | 약 120,700원 | 같은 비교 사양 기준 |

상세 계산은 부록 A에 정리한다.

#### 2.3 비용 대비 효용 판단식

상위 DB 구성은 줄일 수 있는 손실이 추가 총비용보다 클 때만 선택한다.

```text
예상 월 손실 회피액
= 장애 1회당 매출 손실 × 월 장애 발생 확률
+ 데이터 복구·정산 비용
+ 고객 지원·환불 비용
+ 장기 이탈로 인한 손실
+ 장애 대응 인건비 절감액

예상 월 추가 비용
= 상위 DB 구성의 월 인프라 비용 차이
+ 애플리케이션 변경비의 월 환산액
+ 추가 운영 복잡도의 월 비용
```

다음 조건을 만족할 때만 상위 구성을 검토한다.

```text
예상 월 손실 회피액 > 예상 월 추가 비용
```

현재는 데이터 복구, 고객 지원, 환불, 장기 이탈과 장애 대응 인건비 자료가 없다. 따라서 직접 매출 손실을 우선 계산하고, 나머지 항목은 실제 자료가 확보되면 추가한다. 측정되지 않은 편익은 없다고 단정하지 않고, **현재 계산에는 0원으로 반영한다.**

### 3. 애플리케이션과 DB 분리 필요성

#### 3.1 처리량만으로는 분리 여부를 결정할 수 없다

V2의 운영 계획은 약 200 QPS이고 검증 계획은 약 400 QPS다. 이 수치만으로 RDS, Read Replica 또는 Aurora가 반드시 필요하다고 결론 내릴 수 없다.

쿼리 수가 많아도 인덱스와 실행 계획이 적절하면 하나의 DB 인스턴스에서 처리할 수 있다. 반대로 QPS가 낮아도 Full Scan, 잠금 경합 또는 부족한 메모리 때문에 병목이 생길 수 있다. 따라서 DB 분리와 상위 사양 도입 여부를 QPS만으로 결정하지 않는다.

#### 3.2 V2 구조에서는 DB 분리가 필요하다

V1처럼 애플리케이션과 MySQL을 하나의 EC2에서 운영하면 다음 문제가 발생한다.

- 애플리케이션 EC2를 여러 대로 확장해도 로컬 DB를 공동으로 사용할 수 없다.
- 애플리케이션과 MySQL이 CPU, 메모리와 디스크 I/O를 경쟁한다.
- EC2 장애가 API 장애와 DB 장애를 동시에 일으킨다.
- 애플리케이션과 DB를 독립적으로 확장할 수 없다.
- 백업·복원·패치와 장애 대응이 애플리케이션 운영과 결합된다.

V2는 여러 애플리케이션 인스턴스가 하나의 서비스 데이터를 사용하고 애플리케이션과 DB를 독립적으로 확장하는 구조를 목표로 한다. 따라서 **애플리케이션과 DB의 분리는 기능상 필수다.**

### 4. EC2 직접 운영과 RDS 비교

이 단계에서는 이중화 여부를 결정하지 않는다. 먼저 DB 운영 책임을 팀이 직접 담당할지 AWS 관리형 서비스에 맡길지 판단한다. 비교할 때는 단일 구성끼리, Primary–Standby 구성끼리 비교한다.

#### 4.1 공통 요구사항

어느 운영 방식을 선택하든 다음 조건을 충족해야 한다.

- 여러 애플리케이션 인스턴스가 같은 데이터에 접근해야 한다.
- 애플리케이션과 DB의 CPU·메모리·스토리지를 독립적으로 확장할 수 있어야 한다.
- DB는 Private Network에서만 접근하고 스토리지 암호화와 TLS를 적용해야 한다.
- 백업과 특정 시점 복구 절차를 제공하고 실제 복원을 정기적으로 검증해야 한다.
- CPU·메모리·스토리지·연결 수·쿼리 지연·교착 상태·Slow Query를 관측해야 한다.
- 3년 누적 데이터로 약 200 QPS를 운영 시험하고 약 400 QPS를 검증해야 한다.

#### 4.2 운영 책임 비교

| 운영 항목 | EC2에서 MySQL 직접 운영 | RDS MySQL |
| --- | --- | --- |
| OS 설치·보안 패치 | 팀이 수행 | AWS 관리 영역 |
| MySQL 설치·Minor Patch | 팀이 수행 | 유지보수 정책에 따라 관리 |
| 백업·PITR 구성 | 팀이 Binlog·Snapshot·보존 정책 구성 | 자동 백업과 PITR 사용 |
| 복원 검증 | 팀이 수행 | 팀이 수행 |
| 인스턴스·스토리지 모니터링 | 팀이 직접 구성 | RDS와 CloudWatch 지표 사용 |
| 쿼리·스키마 최적화 | 팀이 수행 | 팀이 수행 |
| 단일 DB 장애 복구 | 인스턴스 교체·EBS 연결·복원을 직접 수행 | RDS 복구 절차 사용 |
| Primary–Standby 복제 | 팀이 구성하고 검증 | Multi-AZ에서 관리 |
| 장애 감지·승격 | 팀이 구현하고 운영 | Multi-AZ에서 자동 처리 |
| Fencing·Split-brain 방지 | 팀 책임 | 관리형 장애 전환 범위 |
| Root·OS 접근과 임의 Plugin | 가능 | 제한됨 |

EC2의 낮은 인프라 비용이 실제 절감으로 이어지려면 팀이 다음 운영 책임을 수행할 수 있어야 한다.

- Snapshot과 Binlog 백업이 자동으로 실행되고 보존·실패 알람이 동작해야 한다.
- 빈 인스턴스에서 목표 시점까지 복원하는 절차와 소요시간을 검증해야 한다.
- OS·MySQL 보안 패치 일정과 Rollback 절차가 있어야 한다.
- CPU·메모리·디스크·연결 수·복제 상태·Slow Query 알람이 있어야 한다.
- 야간·휴일 장애 담당자와 복구 절차가 정해져 있어야 한다.

#### 4.3 RDS 관리형 프리미엄

단일 DB 기준으로 RDS는 EC2 직접 운영보다 월 약 59,400원 비싸다. 운영 인력의 시간당 총비용을 3만-5만 원으로 가정하면 비용 회수에 필요한 순운영시간 절감량은 월 약 1.2-2.0시간이다.

| 비교 | 월 프리미엄 | 시간당 30,000원 | 시간당 50,000원 |
| --- | ---: | ---: | ---: |
| EC2 단일 DB → RDS Single-AZ | 약 59,400원 | 약 2.0시간/월 | 약 1.2시간/월 |
| EC2 Primary–Standby → RDS Multi-AZ | 최대 약 124,600원 | 약 4.2시간/월 | 약 2.5시간/월 |

이 값은 RDS가 실제로 절감하는 시간이 아니라 관리형 프리미엄을 회수하기 위한 손익분기점이다. 전체 DB 운영시간이 아니라 **RDS로 전환했을 때 실제로 사라지는 순운영시간**만 계산해야 한다.

쿼리·스키마 최적화, 복원 검증, 애플리케이션 재연결과 장애 훈련은 RDS에서도 필요하므로 절감시간에 포함하지 않는다.

#### 4.4 운영 방식 결정

현재 확인된 값은 EC2 단일 DB 월 61,300원과 RDS Single-AZ 월 120,700원이다. RDS가 줄일 수 있는 순운영시간과 EC2 고유 장애 손실은 측정되지 않았다.

따라서 **V2의 잠정 기본 운영 방식은 EC2에서 MySQL을 직접 운영하는 방식**으로 결정한다. 다음 식으로 계산한 RDS의 월 편익이 59,400원을 넘을 때 도입 여부를 재검토한다.

```text
RDS 월 편익
= RDS로 실제 제거되는 순운영시간 × 시간당 총인건비
+ EC2에서만 발생할 것으로 예상되는 월 장애 손실
+ EC2 전용 백업·패치·모니터링 도구 비용
```

다음 중 하나를 내부 자료로 입증하면 RDS 도입 여부를 다시 검토한다.

1. RDS가 실제로 제거하는 순운영시간이 월 1.2~2.0시간을 넘는다.
2. EC2 DB 장애·패치·복구의 월 기대 손실을 포함한 편익이 59,400원을 넘는다.
3. 감사·규정 또는 필수 RTO·RPO 때문에 관리형 기능이 필요하다.

EC2 운영 검증에 실패하면 보완 비용을 EC2 총소유비용에 포함해 RDS와 다시 비교한다. Root 접근이나 RDS 미지원 Plugin이 필요하다면 비용과 별개로 EC2를 선택해야 하는 사유가 될 수 있다.

### 5. 단일 DB와 Primary–Standby 비교

#### 5.1 구성 차이

| 항목 | 단일 DB | Primary–Standby |
| --- | --- | --- |
| Writer | 1대 | 1대 |
| 다른 가용 영역의 Standby | 없음 | 1대 |
| 복제 | 해당 없음 | 동기 복제 목표 |
| 장애 대응 | 복원 또는 인스턴스 교체 | 자동 장애 전환 목표 |
| 고정 접속 주소 | 단일 Endpoint | Proxy 또는 관리형 Endpoint 필요 |
| Split-brain 통제 | 해당 없음 | Fencing·Witness 또는 관리형 제어 필요 |
| Standby 읽기 사용 | 해당 없음 | V2 구성에서는 사용하지 않음 |
| EC2 최소 월 비용 | 약 61,300원 | 약 115,700원부터 |
| 월 추가 비용 | 기준 | 최소 약 54,400원 |
| 월 예상 매출 대비 | 약 0.85% | 최소 약 1.61% |

Primary–Standby는 읽기 성능을 높이기 위한 구성이 아니다. 다른 가용 영역에 Standby를 유지해 Primary 또는 가용 영역 장애 시 복구시간을 줄이고 커밋된 데이터를 보호하기 위한 고가용성 구성이다.

EC2 Primary–Standby 비용은 DB 노드와 EBS만 반영한 하한이다. Witness, Fencing, Proxy, 교차 AZ 통신, 장애 감지·승격 자동화와 운영 인건비를 포함하면 실제 비용은 더 커진다.

#### 5.2 직접 매출 기준 손익분기점

월 매출이 한 달 동안 균등하게 발생한다고 가정하면 분당 기대 매출은 약 166.7원이다.

| DB 중단 시간 | 직접 매출 손실 상한 | 같은 장애가 연 1회일 때 월평균 기대 손실 |
| --- | ---: | ---: |
| 5분 | 약 830원 | 약 70원 |
| 30분 | 약 5,000원 | 약 420원 |
| 60분 | 약 10,000원 | 약 830원 |
| 120분 | 약 20,000원 | 약 1,670원 |
| 720분 | 약 120,000원 | 약 10,000원 |

EC2 Primary–Standby의 최소 추가 비용인 월 54,400원을 직접 매출 손실만으로 회수하려면 도입 후 월 중단 시간이 약 326분, 즉 5.4시간 이상 줄어야 한다.

현재는 단일 DB가 Primary–Standby보다 매월 5.4시간 이상 더 중단될 것이라는 장애 이력이 없다. 이 손익분기점도 아직 반영되지 않은 이중화 운영비를 제외한 값이다.

#### 5.3 토폴로지 결정

현재 매출과 직접 중단 손실만 기준으로 하면 **Primary–Standby의 비용 대비 효용을 입증할 수 없다.** 따라서 V2의 잠정 기본 토폴로지는 단일 DB다.

다만 다음 비용은 직접 매출 계산에 포함되지 않았다.

- 주문·결제 상태 누락이나 불일치의 복구 비용
- 환불과 고객 지원 비용
- 장기적인 신뢰 하락과 고객 이탈
- 야간·휴일 장애 대응 인건비
- 계약상 SLA 또는 내부 보안·복구 정책 위반 비용
- 복원 시점 이후 변경 데이터의 재구성 비용

이 비용의 월 기대값이 완전한 Primary–Standby의 추가 총소유비용을 넘거나 `RTO 5분·커밋 완료 데이터 RPO 0분`이 필수 정책이라면 Primary–Standby를 선택한다. 이중화는 성능 투자가 아니라 데이터 보호와 복구시간 제한을 위한 보험 비용으로 본다.

| 의사결정 기준 | V2 선택 |
| --- | --- |
| 현재 매출과 직접 중단 손실만 반영 | **단일 DB** |
| `RTO 5분·커밋 완료 데이터 RPO 0분`이 필수 정책 | **Primary–Standby** |

### 6. 선택 구성과 운영 조건

#### 6.1 현재 기본 구성

```text
사용자
  ↓
ALB
  ├─ 애플리케이션 A
  └─ 애플리케이션 B
        ↓
EC2 MySQL 8.4 단일 DB
        ├─ gp3 100GB·EBS 암호화
        ├─ Snapshot·Binlog 보관과 복원 절차
        └─ 모니터링·알람
```

#### 6.2 운영 적용 원칙

- DB EC2는 애플리케이션 EC2와 분리해 Private Subnet에 배치한다.
- DB 포트는 애플리케이션 Security Group에서만 접근할 수 있도록 제한한다.
- Public IP와 SSH 직접 접속을 차단하고 SSM 등 통제된 운영 경로를 사용한다.
- EBS Snapshot과 Binlog 보존 정책을 자동화하고 특정 시점 복원을 정기적으로 검증한다.
- OS·MySQL 보안 패치, CVE 대응과 재시작 절차를 운영 일정에 포함한다.
- 주문·결제 변경 API에는 Idempotency Key를 적용한다.
- 스키마 변경은 Expand-Contract 방식으로 수행한다.
- CPU·메모리·스토리지·연결 수·읽기 및 쓰기 지연·IOPS·교착 상태·Slow Query를 관측한다.
- 3년 누적 데이터로 약 200 QPS를 운영 시험하고 약 400 QPS를 검증한다.
- EC2 인스턴스와 스토리지는 부하 시험을 통과하는 최소 크기로 조정한다.
- 백업 저장량, Binlog 보관, 모니터링과 운영 인건비를 실제 청구액과 작업시간으로 기록한다.

#### 6.3 Primary–Standby가 필수인 경우

| 항목 | EC2 MySQL Primary–Standby | RDS MySQL Multi-AZ DB Instance |
| --- | --- | --- |
| 인프라 비용 | 월 115,700원부터 | 월 약 240,300원 |
| 추가 비용 | Witness·Fencing·Proxy·교차 AZ 통신·모니터링·운영 인건비 | 초과 백업·고급 모니터링 등 |
| 자동 장애 전환 | 팀이 구현하고 시험으로 입증 | 관리형 장애 전환 제공 |
| 선택 조건 | 완전한 TCO가 더 낮고 RTO·RPO를 시험으로 충족 | EC2가 복구 목표를 입증하지 못하거나 관리형 편익이 비용 차이보다 큰 경우 |

필수 RTO·RPO 때문에 Primary–Standby가 필요해지면 다음 순서로 판단한다.

1. EC2 구성에 Witness·Fencing·Proxy·교차 AZ 통신·모니터링과 운영 인건비를 모두 포함한다.
2. 실제 장애 시험으로 자동 장애 전환과 RTO·RPO 달성 여부를 검증한다.
3. 완전한 EC2 총소유비용과 RDS Multi-AZ의 월 비용을 비교한다.
4. EC2가 복구 목표를 입증하지 못하면 비용이 낮더라도 후보에서 제외한다.

어느 방식을 사용하더라도 애플리케이션의 연결 재수립, 제한된 재시도, 멱등성과 장애 시험이 필요하다. 이중화는 잘못된 SQL이나 논리 삭제도 복제하므로 백업과 PITR을 대체하지 않는다.

### 7. Read Replica 도입 판단

현재 계획값인 200 QPS와 검증값인 400 QPS에서 단일 Writer가 성능 목표를 충족하지 못한다는 시험 결과는 없다. 따라서 초기 V2에는 Read Replica를 추가하지 않는다.

EC2에 같은 사양의 비동기 읽기 Replica 1대를 직접 구성하면 컴퓨팅과 EBS만으로 월 약 54,400원이 추가된다. RDS Read Replica는 월 약 120,700원이 추가된다.

| 항목 | EC2 직접 구성 | RDS Read Replica |
| --- | ---: | ---: |
| Replica 추가 비용 | 약 54,400원부터 | 약 120,700원 |
| 월 예상 매출 대비 | 약 0.76% | 약 1.68% |
| 추가 고려 비용 | 교차 AZ 전송·구성·감시·복구 인건비 | 애플리케이션 변경·모니터링 비용 |

Read Replica를 도입하면 다음 복잡성도 증가한다.

- Writer와 Reader의 연결 풀 분리
- 쿼리별 라우팅 규칙
- 쓰기 직후 조회의 Writer 고정
- Replica Lag 감시
- Reader 장애 시 우회와 부하 제한
- 비동기 복제로 인한 오래된 데이터 조회 처리

외부 성능 개선 사례만으로 Replica의 편익을 인정하지 않는다. 다음 조건을 모두 확인한 경우에만 도입 여부를 재검토한다.

1. 쿼리·인덱스·페이징과 N+1 문제를 먼저 개선했다.
2. 개선 후에도 Writer의 CPU·IOPS·연결 수 또는 P95 쿼리 지연이 목표를 지속적으로 위반한다.
3. 지연을 허용하는 조회가 병목의 주원인이다.
4. 병목으로 인한 월 손실이 Replica 비용과 애플리케이션 운영비보다 크다.
5. Replica Lag을 허용할 수 있는 조회 범위를 명확히 분리할 수 있다.

### 8. 상위 사양 검토 조건

Read Replica가 필요하지 않은 현재 단계에서는 Multi-AZ DB Cluster, Aurora MySQL, RDS Proxy와 외부 Cache를 상세 비교하지 않는다. 다음 문제가 실제로 확인되고 선행 조치로 해결되지 않을 때만 관련 대안을 검토한다.

| 확인된 문제 | 우선 조치 | 해결되지 않을 때 검토할 후보 |
| --- | --- | --- |
| 느린 조회 | 실행 계획·인덱스·페이징·N+1 개선 | Read Replica 또는 Cache |
| Writer CPU·잠금 병목 | 쿼리·트랜잭션·잠금 범위 개선 | Writer Class·스토리지 확장 |
| 연결 고갈 | 연결 풀 총량과 Timeout 조정 | RDS Proxy |
| 반복되는 Hot Query | 쿼리·인덱스 개선 | Redis·Valkey Cache |
| 읽기 가능한 장애 전환 후보가 2대 이상 필요 | 요구사항과 손실 재산정 | Multi-AZ DB Cluster |
| 다수 Reader와 더 높은 스토리지 내구성 필요 | 요구사항과 Migration 비용 재산정 | Aurora MySQL |

### 9. 재검토를 위한 측정 항목

현재 결정은 실제 운영 자료가 확보되면 다시 평가해야 한다. 다음 항목을 지속적으로 기록한다.

- EC2 DB 운영에 사용한 월 작업시간과 시간당 총인건비
- 백업·패치·모니터링 도구의 실제 비용
- 장애 시작·종료 시각과 실제 사용자 영향 시간
- 장애 중 영향을 받은 순사용자와 Session 수
- 실패한 로그인·검색·장바구니·주문·결제 건수
- 재시도로 복구된 요청과 최종 실패 요청 수
- 장애 전후 7일·30일·90일 재방문율과 구매전환율
- 환불·고객 문의·수동 정산 건수
- 장애 대응과 데이터 복구에 투입한 인원과 시간
- 시간대별 실제 매출과 제휴수수료
- DB 쿼리 P95·P99와 API 응답시간
- Replica로 분리할 수 있는 조회 비율과 허용 가능한 Replica Lag

다음 중 하나가 확인되면 관련 결정을 재검토한다.

| 확인 사항 | 재검토 대상 |
| --- | --- |
| RDS의 월 편익이 59,400원을 초과 | EC2 직접 운영 → RDS Single-AZ |
| 완전한 Primary–Standby의 손실 회피액이 추가 TCO를 초과 | 단일 DB → Primary–Standby |
| 필수 RTO·RPO 정책 도입 | EC2 Primary–Standby와 RDS Multi-AZ 재비교 |
| EC2가 자동 장애 전환과 복구 목표를 입증하지 못함 | RDS Multi-AZ 선택 |
| 최적화 후에도 읽기 병목의 월 손실이 Replica 총비용을 초과 | Read Replica 검토 |

### 부록 A. 상세 비용 계산

AWS 공식 Price List에서 확인한 비교 단가는 다음과 같다.

| 항목 | 단가 |
| --- | ---: |
| EC2 Linux `t4g.medium` | $0.0416/시간 |
| EBS gp3 | $0.0912/GB-월 |
| EBS Snapshot | $0.05/GB-월 |
| Single-AZ `db.t4g.medium` | $0.102/시간 |
| Multi-AZ `db.t4g.medium` | $0.203/시간 |
| Single-AZ gp3 | $0.131/GB-월 |
| Multi-AZ gp3 | $0.262/GB-월 |

```text
EC2 MySQL 단일 DB
= ($0.0416 × 730시간) + ($0.0912 × 100GB) + ($0.05 × 100GB Snapshot)
= $44.49
= 약 61,300원/월

EC2 MySQL Primary–Standby 최소 인프라
= 2 × (($0.0416 × 730시간) + ($0.0912 × 100GB))
+ ($0.05 × 100GB Snapshot)
= $83.98
= 약 115,700원/월

RDS Single-AZ
= ($0.102 × 730시간) + ($0.131 × 100GB)
= $87.56
= 약 120,700원/월

RDS Multi-AZ DB Instance
= ($0.203 × 730시간) + ($0.262 × 100GB)
= $174.39
= 약 240,300원/월

같은 사양의 RDS Single-AZ Read Replica 1대
= $87.56
= 약 120,700원/월
```

EC2 Primary–Standby의 최소 월 추가 비용을 직접 매출만으로 회수하기 위한 중단 감소시간은 다음과 같다.

```text
분당 기대 매출
= 7,200,000원 ÷ (30일 × 24시간 × 60분)
= 약 166.7원

손익분기 중단 감소시간
= 54,400원 ÷ 166.7원/분
= 약 326분
= 약 5.4시간/월
```

### 부록 B. 외부 벤치마크 적용 원칙

외부 자료는 장애 비용의 방향과 민감도를 확인하는 데만 사용한다. 조사 대상의 국가·기업 규모·서비스 유형·질문 방식이 V2와 다르므로 외부 비율을 실제 이탈률로 직접 적용하지 않는다.

- AWS RDS SLA는 RDS를 선택한 조건부 경로에서 Single-AZ와 Multi-AZ의 민감도를 비교할 때만 사용한다. EC2 직접 운영의 예상 가동률로 사용하지 않는다.
- FullStory와 PwC의 고객 이탈 관련 수치는 실제 행동 관측값이 아닌 응답 의향이 포함되어 있으므로 상한 시나리오로만 사용한다.
- Farfetch의 성능 개선 사례는 Frontend와 상품 페이지 전반의 개선 결과이므로 Read Replica의 직접 효과로 간주하지 않는다.
- Uptime Institute의 장애 비용 자료는 대형 기업의 중대 장애 표본이므로 V2 비용 계산에 직접 대입하지 않는다.

AWS SLA는 실제 기대 가동률이나 허용 가능한 최대 중단시간이 아니라 Service Credit의 기준이다. 따라서 장애 예측값이 아니라 비교 민감도에만 사용한다.

외부 자료보다 다음 내부 데이터를 우선한다.

1. 실제 장애 중 발생한 주문·결제 실패와 매출 손실
2. 장애 시간대에 영향을 받은 순사용자 수
3. 영향 사용자와 비영향 사용자의 7일·30일·90일 재방문율 및 구매율 차이
4. 환불·고객 지원·정산과 장애 대응에 사용된 비용

내부 이탈 손실은 외부 설문의 전체 이탈 응답률이 아니라 같은 기간 비영향 사용자와 비교한 **추가 이탈률**로 계산한다.

```text
장애로 인한 미래 매출 손실
= 영향을 받은 순사용자 수
× 비영향 사용자 대비 추가 이탈률
× 사용자당 월 기대 매출
× 영향 지속 개월
```

### 참고 자료

- [AWS RDS와 EC2의 관리 책임 비교](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Welcome.html)
- [AWS EC2 On-Demand 요금과 교차 AZ 데이터 전송](https://aws.amazon.com/ec2/pricing/on-demand/)
- [AWS 공식 Price List: 서울 리전 Amazon EC2](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/ap-northeast-2/index.csv)
- [AWS RDS 요금과 Multi-AZ 복제 데이터 전송](https://aws.amazon.com/rds/pricing/)
- [AWS RDS for MySQL 요금](https://aws.amazon.com/rds/mysql/pricing/)
- [AWS 공식 Price List: 서울 리전 Amazon RDS](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/ap-northeast-2/index.json)
- [AWS RDS Service Level Agreement](https://aws.amazon.com/rds/sla/)
- [AWS RDS Multi-AZ DB Instance](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZSingleStandby.html)
- [AWS RDS Read Replica](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_ReadRepl.html)
- [AWS RDS Backup과 PITR](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html)
- [FullStory: Consumer Digital Experience Survey 2021](https://www.prnewswire.com/news-releases/new-consumer-study-from-fullstory-finds-stakes-higher-than-ever-when-it-comes-to-digital-experience-301285433.html)
- [PwC: Customer Loyalty Survey 2022 인용](https://www.pwc.com/us/en/technology/alliances/library/adobe-personalization-and-trust-in-business.html)
- [PwC: Customer Experience Survey 2025](https://www.pwc.com/us/en/services/consulting/commercial-excellence/library/2025-customer-experience-survey.html)
- [web.dev: Farfetch 성능과 전환율 사례](https://web.dev/case-studies/farfetch)
- [Uptime Institute: Annual Outage Analysis 2024](https://intelligence.uptimeinstitute.com/resource/annual-outage-analysis-2024)
