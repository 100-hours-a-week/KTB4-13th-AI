---
wiki: CLD-1 인프라 구성 및 선택 근거
type: design
group: cld-1
owner: 미정
status: 작성중
updated: 2026-09-17
sources:
  - 1-2-인프라-구성-및-선택-근거.md
order: 2
---
**요약** 클라우드는 AWS, 서버는 App EC2·AI EC2 2대로 구성하며, 개인정보·장기보관 데이터의 저장소와 Reverse Proxy·네트워크·IAM 선택 근거를 정리한다.

## 1. 인프라 설계 기준

인프라를 구성할 때 다음 항목을 고려한다.

- 비용
- 빠른 구축
- 개인정보 보안

V1은 약 3주간 운영되는 초기 POC이며, 일반 Peak Traffic은 약 **0.18 RPS**, 홍보 직후 평균 Traffic은 약 **0.82 RPS**로 예상한다.

현재 예상 트래픽이 1 RPS 미만이므로 높은 사양이나 복잡한 인프라보다 비용과 구축 편의성을 우선한다.

## 2. 클라우드 서비스 선정

클라우드 서비스 비교는 2 vCPU·2 GiB를 기준으로 한다. Azure는 동일한 사양이 없어 2 vCPU·4 GiB를 기준으로 비교한다.

JVM 기반 애플리케이션을 운영할 때 1 vCPU·1 GiB는 여유가 부족할 수 있으므로 2 vCPU·2 GiB를 비교 사양으로 설정한다.

- OS가 일정 수준의 메모리를 사용
- JVM과 Spring Boot가 Heap 외에도 Metaspace, Thread Stack, 내장 Tomcat 등의 메모리를 사용
- MySQL이 버퍼와 연결을 위한 메모리를 사용
- Docker 실행에 필요한 자원 사용

AI 추천은 별도의 AI EC2에서 처리하므로 App EC2에는 별도의 모델 메모리가 필요하지 않다.

그 이상의 사양은 현재 예상 트래픽에 비해 과도하다고 판단한다.

### 2.1. 비용 비교

| 클라우드 | 서버 사양 | 제공 크레딧 | 시간당 요금 | 월 예상 요금 | 금액상 사용 기간 | 실제 사용 가능 기간 |
|---|---|---:|---:|---:|---:|---:|
| AWS | `t3.small` 2 vCPU·2 GiB | 기본 $100 / 최대 $200 | $0.026 | $18.98 / 약 26,600원 | 5.27개월 / 10.54개월 | 기본 약 5.3개월 / 최대 약 10.5개월 |
| GCP | `e2-highcpu-2` 2 vCPU·2 GiB | $300 | $0.06353106 | $46.38 / 약 64,900원 | 약 6.47개월 | 크레딧 만료로 최대 90일 |
| Azure | `B2als v2` 2 vCPU·4 GiB | $200 | $0.0468 | $34.16 / 약 47,800원 | 약 5.85개월 | 크레딧 만료로 최대 30일 |
| 네이버클라우드 | `Compact-g1` 2 vCPU·2GB·50GB | 100,000원 | 64원 | 46,000원 | 약 2.17개월 | 약 65일 |

<details>
<summary><strong>가격 및 크레딧 비교 근거 자료</strong></summary>

<br>

아래 이미지는 비교 당시 확인한 가격 및 크레딧 정보이다.

<img width="851" height="208" alt="image" src="https://github.com/user-attachments/assets/97824a75-c46c-4221-ae2b-ad303e111e70" />
<img width="1196" height="261" alt="image1" src="https://github.com/user-attachments/assets/832fc6a5-e0e3-47c1-80f4-7d23af0ebc58" />
<img width="1344" height="577" alt="image2" src="https://github.com/user-attachments/assets/1f8cf49f-e387-4bc5-a11f-6dc54e0740cb" />
<img width="669" height="681" alt="image3" src="https://github.com/user-attachments/assets/b967efd2-2f12-4193-9a10-e1d40c9eb052" />
<img width="1315" height="504" alt="4" src="https://github.com/user-attachments/assets/cfa7ffaf-4c20-4a2a-ad49-b27b434fb433" />
<img width="1284" height="400" alt="5" src="https://github.com/user-attachments/assets/d7778de4-f7b1-47ca-9ffc-042a2cc8cdd0" />
<img width="958" height="121" alt="6" src="https://github.com/user-attachments/assets/9755caab-8697-40ae-8c80-f3ffcb13fde3" />

</details>

### 2.2. 비교 결과

#### 비용

AWS, GCP, Azure 모두 신규 고객에게 제공되는 무료 크레딧을 적용하면 V1 기간의 실질적인 인프라 비용 차이는 크지 않다.

다만 동일한 2 vCPU·2 GiB 수준의 사양을 기준으로 비교하면 AWS가 가장 저렴하다.

- AWS: 최대 $200 크레딧 / 6개월
- GCP: $300 크레딧 / 90일
- Azure: $200 크레딧 / 30일

GCP는 크레딧 금액이 가장 크지만 90일 제한이 있어 서버 한 대만으로는 기한 내 전액 활용하기 어렵다. Azure는 30일 제한으로 인해 장기 POC보다 단기 실험에 적합하다.

네이버클라우드는 약 65일간 사용할 수 있고 50GB 디스크가 요금에 포함되지만, 크레딧 종료 후 월 서버 비용이 AWS보다 높다.

#### 성능

- 예상 Peak Traffic이 1 RPS 미만
- 2 vCPU·2 GiB급이라면 비교 대상 모두 충분
- 성능 차이는 결정적인 선정 기준이 아님

#### 가용성

V1은 일시적인 장애를 허용할 수 있는 POC이므로 CSP별 SLA 차이는 중요한 선정 기준이 아니다.

| CSP | 단일 VM | 다중 Zone/AZ |
|---|---:|---:|
| AWS EC2 | **99.5%** | **99.99%** |
| GCP | 일반 단일 인스턴스 **99.9%** | **99.99%** |
| Azure | 단일 VM은 디스크 구성 등에 따라 다름 | 2개 이상 AZ 배포 시 **99.99%** |

#### 보안

개인정보 보호는 중요하지만, 비교 대상 모두 필요한 기본 보안 기능을 제공한다.

### 2.3. 결론

무료 크레딧을 오래 활용할 수 있고 크레딧 종료 후 유지 비용도 가장 저렴한 **AWS**를 선택한다.

또한 빠른 구축, 팀 숙련도 및 관리 편의성을 고려했을 때 AWS가 현재 서비스에 적합하다고 판단한다.

## 3. 개인정보 및 장기 데이터 보관

개인정보는 현재 서비스에서 사용하는 운영 데이터와 법적으로 장기 보존해야 하는 거래기록으로 구분하여 관리한다.

### 3.1. 개인정보 보관 기준

개인정보를 처리한다는 이유만으로 개인정보 전용 DB 서버나 별도의 RDS·EC2를 반드시 구축해야 하는 것은 아니다.

다만 회원 탈퇴나 보유기간 만료로 파기해야 하는 개인정보라도 다른 법률에 따라 계속 보존해야 하는 경우에는 현재 이용자의 운영 데이터와 분리하여 관리해야 한다.

도서 판매·배송 서비스는 전자상거래법에 따라 다음 기록을 보존한다.

| 보존 대상 | 보존 기간 |
|---|---:|
| 계약·청약철회 기록 | 5년 |
| 결제·재화 공급 기록 | 5년 |
| 소비자 불만·분쟁 처리 기록 | 3년 |
| 표시·광고 기록 | 6개월 |

전체 회원정보가 아니라 법적으로 보존해야 하는 최소한의 거래기록만 보관한다. 이를 위해 별도의 물리 서버나 DB 인스턴스를 반드시 구축할 필요는 없다.

기존에는 휴면회원 정보를 별도의 DB 등에 분리하여 저장해야 했지만, 2026년 현재 해당 제도가 폐지되었으므로 별도로 고려하지 않는다.

### 3.2. 저장소 검토

장기 보관 데이터를 EC2의 인스턴스 스토어에 저장하는 것은 적합하지 않다.

인스턴스 스토어는 연결된 인스턴스의 수명 동안에만 존재하며, 인스턴스가 중지·최대 절전·종료되면 데이터가 유지되지 않는다.

따라서 운영 데이터와 장기 보관 데이터는 EBS·EFS·S3와 같은 영구 저장소에 저장해야 한다.

| 저장소 | 특징 | 적합한 용도 |
|---|---|---|
| EBS | EC2에 연결하여 빠르게 읽고 쓰는 블록 스토리지이며 스냅샷을 이용해 백업 가능 | MySQL 운영 데이터 |
| EFS | 여러 서버에서 동시에 사용할 수 있는 공유 파일 시스템이며 장기 보관용으로는 S3보다 비용이 높음 | 여러 서버가 공유하는 운영 파일 |
| S3 | 자주 수정하지 않는 데이터를 저렴하게 보관할 수 있고 Lifecycle을 이용한 자동 삭제 가능 | 법적으로 장기 보존해야 하는 거래기록 |

### 3.3. 저장소 비용 비교

| 구분 | EBS `gp3` | EFS Archive | S3 Glacier Flexible |
|---|---:|---:|---:|
| 접근성 | 즉시 접근 | 즉시 접근 | 복원에 약 3~5시간 |
| 서울 리전 요금 | $0.0912/GB·월 | $0.01/GB·월 | $0.0045/GB·월 |
| 10GB 보관 비용 | 약 $0.912/월 | 약 $0.10/월 | 약 $0.045/월 |

운영 중 지속적인 읽기와 쓰기가 필요한 데이터는 EBS에 저장한다.

법적으로 보존해야 하지만 자주 조회하지 않는 거래기록은 비용과 기능을 고려하여 S3에 저장한다.

### 3.4. 결론

| 구분 | 저장 위치 |
|---|---|
| 현재 서비스의 운영 데이터 | EBS |
| 법적으로 장기 보존해야 하는 거래기록 | S3 |
| 개인정보 전용 DB | 별도로 구축하지 않음 |

<details>
<summary><strong>개인정보 및 저장소 참고 자료 보기</strong></summary>

<br>

#### 개인정보 보관 기준

- [국가법령정보센터 조문정보](https://law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1029332029)
- [국가법령정보센터 연계정보](https://law.go.kr/LSW/lsLinkCommonInfo.do?lspttninfSeq=66999&chrClsCd=)
- [다른 법률에 따른 개인정보 보존 기준](https://www.law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lsJoLnkSeq=1029335627)
- [전자상거래 기록 보존기간](https://law.go.kr/LSW/lsLinkCommonInfo.do?chrClsCd=010202&lspttninfSeq=63460)
- [개인정보보호위원회 휴면회원 제도 안내](https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?bbsId=BS074&mCode=C020010000&nttId=9819#LINK)

#### 저장소 검토

- [AWS EC2 인스턴스 스토어 수명](https://docs.aws.amazon.com/ko_kr/AWSEC2/latest/UserGuide/instance-store-lifetime.html)
- [AWS EBS 스냅샷](https://docs.aws.amazon.com/en_en/ebs/latest/userguide/ebs-snapshots.html)
- [EFS 참고 자료](https://wikidocs.net/399851)
- [AWS S3 데이터 내구성](https://docs.aws.amazon.com/ko_kr/AmazonS3/latest/userguide/DataDurability.html)

</details>

## 4. 인프라 구성

V1은 일반 Peak Traffic 약 **0.18 RPS**, 홍보 직후 평균 Traffic 약 **0.82 RPS**로 예상 트래픽이 작다.

따라서 비용과 구축 복잡도를 줄이기 위해 필요한 서비스만 사용한다.

### 4.1. 전체 아키텍처
<img alt="아키텍처1" src="https://github.com/user-attachments/assets/00d44704-79a5-4633-b24f-e60e436dfd81" />

- Frontend: S3, CloudFront
- App EC2: Nginx, Spring Boot, MySQL
- AI EC2: AI Embedding Server, Vector DB

### 4.2. EC2 구성

`t3.small` EC2 인스턴스 2대를 사용한다.

| 구성 | 총 사양 | 시간당 요금 |
|---|---|---:|
| `t3.medium` 1대 | 2 vCPU / 4 GiB | $0.052 |
| `t3.small` 2대 | 4 vCPU / 총 4 GiB | $0.052 |

두 구성의 시간당 요금은 같지만, `t3.small` 2대를 사용하면 동일한 총 메모리에서 더 많은 vCPU를 확보할 수 있다.

또한 애플리케이션과 AI 기능을 하나의 서버에서 함께 운영하면 AI 작업의 자원 사용량이 증가할 때 전체 서비스 성능에 영향을 줄 수 있다. 따라서 서버를 역할별로 분리한다.

| 서버 | 구성요소 | 역할 |
|---|---|---|
| App EC2 | Nginx, Spring Boot, MySQL | 애플리케이션과 데이터베이스 운영 |
| AI EC2 | AI Embedding Server, Vector DB | AI 추천과 벡터 검색 처리 |

App EC2와 AI EC2를 분리하여 각각의 역할에 필요한 자원을 확보한다.

### 4.3. Frontend 배포 - S3 + CloudFront

React 빌드 결과물은 S3에 배포하고 CloudFront를 통해 제공한다.

#### 근거

- React의 HTML, CSS, JavaScript는 정적 파일이므로 별도의 서버 실행 환경이 필요하지 않음
- CloudFront에 정적 파일과 이미지를 캐싱하여 응답 속도를 개선할 수 있음
- App EC2가 Frontend 파일 요청을 처리하지 않아도 되므로 서버 역할을 분리할 수 있음

초기 트래픽은 적지만 정적 파일과 이미지의 비중이 높은 서비스이므로, 사용자에게 안정적이고 빠르게 콘텐츠를 제공하기 위해 S3와 CloudFront를 사용한다.

장기 보관 데이터는 EC2와 분리하여 S3에 저장한다.

### 4.4. Reverse Proxy - Nginx

#### Nginx와 Caddy 비교

| 기준 | Nginx | Caddy |
|---|---|---|
| HTTPS 설정 | Certbot 설정 필요 | 자동 HTTPS 지원 |
| 설정 복잡도 | 상대적으로 높음 | 설정이 단순함 |
| 성능 | 현재 트래픽에 충분함 | 현재 트래픽에 충분함 |
| 팀 숙련도 | 높음 | 상대적으로 낮음 |

#### 선택 결과

Reverse Proxy는 **Nginx + Certbot**을 사용한다.

Caddy는 HTTPS 인증서 발급과 갱신이 자동으로 처리되어 설정이 간단하다는 장점이 있다. 하지만 현재 서비스는 EC2 수가 적고 도메인도 1개이므로 Nginx와 Certbot으로도 HTTPS를 어렵지 않게 적용할 수 있다.

또한 팀이 Nginx 운영에 익숙하므로 새로운 도구를 도입하는 것보다 기존 경험을 활용하는 편이 효율적이라고 판단한다.

### 4.5. 네트워크 및 접근 제어

#### VPC

- Default VPC 사용
- EC2는 Public Subnet에 배치
- 별도의 Private Subnet, NAT Gateway, Bastion Host는 구성하지 않음

이름, 주소, 전화번호 등 개인정보는 다루지만 주민등록번호, 금융정보, 의료정보처럼 높은 수준의 보호가 필요한 고위험 정보는 다루지 않는다.

V1에서는 인프라를 빠르게 구축하는 것이 중요하므로 별도의 Private Subnet, NAT Gateway, Bastion Host를 구성하지 않는다.

대신 EC2가 외부에 직접 노출되는 만큼 보안그룹을 통해 필요한 포트만 허용하고 HTTPS를 적용하여 기본적인 보안 수준을 유지한다. App EC2와 AI EC2는 VPC 내부에서 통신한다.

#### 보안그룹

| 포트 | 용도 | 접근 범위 |
|---:|---|---|
| `80` | HTTP 요청을 HTTPS로 Redirect | 전체 사용자 |
| `443` | HTTPS 통신 | 전체 사용자 |
| `22` | SSH 접속 | 지정된 관리자 IP |

#### Route 53

- 도메인을 서비스에 연결하기 위해 사용
- AWS 내에서 DNS를 함께 관리하여 초기 설정과 운영을 단순화

#### Elastic IP

- App EC2의 공인 IP가 변경되지 않도록 고정 IP 사용
- AI EC2는 사용자에게 직접 노출하지 않고 App EC2와 VPC 내부에서 통신

### 4.6. IAM 권한 관리

- AWS 리소스를 직접 관리하는 인원: 인프라 담당 2명
- 나머지 팀원: 장애 확인을 위한 CloudWatch 로그 조회 권한만 제공

V1에서 AWS 리소스를 직접 관리하는 인원은 인프라 담당 2명이다.

다른 팀원은 인프라를 변경할 필요가 없고 장애 확인을 위한 로그 조회가 주 목적이므로, CloudWatch 조회 권한만 가진 IAM 계정을 제공한다.

권한을 단순하게 유지하여 계정 관리와 설정 시간을 줄인다.
