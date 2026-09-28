---
wiki: FS-3 기술 스택 정의
type: design
group: fs-3
owner: 김세훈
status: 작성중
updated: 2026-09-28
sources:
  - "[3단계]-기술-스택-정의서.md"
---
**요약** Java 25·Gradle·Spring Boot·MySQL(InnoDB)·Redis 선택 이유와 서비스 적용 지점을 정리한 기술 스택 정의서다.

##### Language: Java 25 - [Java 25](#java-25)
##### Build: Gradle - [빌드: Gradle](#빌드-gradle)
##### Framework: Spring 4.1.0 - [프레임워크: Spring Boot](#프레임워크-spring-boot)
##### Database: MySQL 8.1.14 - [MySQL(InnoDB)](#mysqlinnodb)

## Java 25

#### Java 사용 이유

- OOP: 코드를 독립된 객체 단위로 나누어 관리하므로 코드 재사용성과 유지보수성을 챙길 수 있다.
- JVM:
  - GC: 자동 메모리 관리를 하여 개발자가 메모리 해제 대신 비즈니스 로직 구현에 집중할 수 있다.
  - JIT Compiler: 전체 코드를 미리 다 컴파일하지 않고, 자주 쓰이는 부분만 골라서 컴파일하므로 불필요한 자 원 낭비를 줄인다. 정적 컴파일보다 정교한 최적화를 수행한다.
  - JVM 진단 도구: 메모리 덤프와 스레드 덤프를 심층 분석할 수 있다.
- 정적 타입: 타입 안정성에 대한 검증을 컴파일 시점에서 검증 가능하다.

#### Java25 사용 이유

#### Virtual Thread
##### 개념 요약
- Virtual Thread는 JVM이 생성하고 스케줄링하는 `java.lang.Thread`이다.
  - Virtual Thread는 실행할 때만 Carrier Platform Thread를 사용한다.
- 특정 OS Thread와 수명 전체가 1:1로 연결되지 않아 생성과 대기 비용이 Platform Thread보다 저렴하다.
- 동기식 MVC 코드 구조를 유지하면서 I/O 대기 중심 서비스의 동시 처리량을 높일 수 있다.

##### 특징
- Virtual Thread는 특정 OS Thread를 직접 점유하지 않는다.
- 실행할 때만 Platform Thread인 carrier에 mount된다.
  - 지원되는 Blocking I/O가 발생하면 carrier에서 unmount되어 JVM scheduler의 대기 상태로 전환된다.
  - I/O가 완료되면 같은 carrier 또는 다른 carrier에 다시 mount되어 실행을 이어간다.
  - 이처럼 반납된 carrier에서는 다른 Virtual Thread를 실행할 수 있다.
- JDBC, JPA, 외부 HTTP API처럼 기존의 동기식 Blocking API를 그대로 사용할 수 있다.
- 필요할 떄만 Platform Thread 위에서 작업하고, Blocking이 발생하면 바로 JVM 스케줄러에게 반납한다.
- JVM이 필요한 순간에만 Virtual Thread → Platform Thread를 바인딩한다.

<p align="center">
  <img src="https://github.com/user-attachments/assets/12b31266-1f1f-40bb-9bdf-3850b05824eb" width="49%" />
  <img src="https://github.com/user-attachments/assets/9f48efdd-1e00-47f2-ac28-3892b945e26c" width="49%" />
</p>

##### 참고
- https://docs.oracle.com/en/java/javase/25/core/virtual-threads.html
- https://openjdk.org/jeps/444
- https://openjdk.org/jeps/491

##### 주의점

<details><summary>Virtual Thread의 pinning 현상</summary>

JDK 21에서는 synchronized 내부에서 Blocking I/O를 수행하거나 monitor 획득을 기다릴 때 Virtual Thread가 carrier에 pinning될 수 있었다.

JDK 24에 적용된 JEP 491부터는 다음 상황에서도 Virtual Thread가 carrier를 반납할 수 있다.
- synchronized 블록이나 메서드 내부의 Blocking
- monitor 획득 대기
- Object.wait() 대기
- monitor 재획득 대기

따라서 Java 25에서는 Virtual Thread의 pinning을 피하기 위해 synchronized를 모두 ReentrantLock으로 변경할 필요가 없다.

다만 Native Method, Foreign Function과 일부 클래스 로딩 과정에서는 여전히 pinning이 발생할 수 있다. Pinning은 오류를 만들지는 않지만, 장시간 반복되면 carrier를 점유하여 처리량을 낮출 수 있다.

</details>

#### Scoped Values

##### 개념 요약
`Scoped Values`는 특정 실행 범위 안에서만 값을 공유할 수 있게 해주는 기능이다.
기존 `ThreadLocal`은 다음과 같은 문제가 있다.
- 요청 처리가 끝난 뒤 `remove()`를 누락하면 같은 스레드에서 처리되는 다음 요청이 이전 값을 읽을 수 있다.
- 값이 스레드에 예상보다 오래 남으면 객체의 수명이 불필요하게 길어져 메모리 누수로 이어질 수 있다.
- 키에 접근할 수 있는 하위 코드가 `set()`으로 값을 변경할 수 있어 데이터 흐름을 추적하기 어렵다.

Scoped Values의 핵심은 값을 정해진 scope 안에서만 안전하게 공유하는 것이다.

##### 특징
- `run()` 또는 `call()`이 실행되는 동안만 값이 바인딩된다.
- 작업이 정상 종료되거나 예외가 발생하면 바인딩도 자동으로 종료된다.
  - `ThreadLocal.remove()`와 같은 명시적인 정리 작업이 필요하지 않다.
- `ThreadLocal`은 키에 접근할 수 있는 코드가 set()으로 값을 바꿀 수 있지만, `ScopedValue`에서는 변경 API를 지원하지 않아 하위 메서드가 바인딩을 변경할 수 없다.

##### 서비스에 적용할 점
- 도서 조회 요청의 requestId와 traceId
- 주문 생성과 결제 요청의 감사 로그 식별자

##### 참고
- https://openjdk.org/jeps/506

#### AOT Cache

##### 개념 요약
클래스 로딩, 링킹 결과와 메서드 실행 `.aot` 캐시 파일에 프로파일을 저장한다. 운영 JVM은 이 캐시를 재사용해 애플리케이션의 시작 작업을 줄이고 JIT 컴파일을 앞당길 수 있다.

##### 특징
- Spring Boot가 실행될 때마다 반복하던 클래스 탐색, 파싱, 로딩과 링킹 작업의 일부를 재사용할 수 있다.
- 기존 JVM은 실제 요청을 처리하면서 자주 실행되는 메서드를 파악했다. Java 25는 사전에 수집한 메서드 프로파일을 활용해 JIT 최적화를 앞당길 수 있다.
- 기존 캐시 프로파일링이 잘못되었어도, 운영 중에서 실제 트래픽을 JVM(JIT Compiler)은 프로파일을 새로 수집하고 코드를 HotSpot기반으로 다시 최적화한다.

##### 참고
- https://openjdk.org/jeps/515

#### Compact Object Headers
##### 개념 요약
64-bit HotSpot JVM의 객체 헤더를 96~128 bit에서 64 bit로 줄이는 기능이다.
- Java Object 의 Header 에 Meta data 를 128 bit -> 64 bit로 축소한다.
- 전체 Heap Memory 사용량이 10~20% 감소했다.
- 객체 크기와 GC 부담이 줄어들면 메모리 지역성과 실행 성능이 개선됐다.

기존에는 객체마다 64-bit 마크 워드(Mark Word)와 별도의 클래스 포인터(Klass Pointer)를 저장했다.
Compact Object Headers는 클래스 포인터를 22 bit로 인코딩하여 마크 워드 안에 함께 저장한다.

<img src="https://github.com/user-attachments/assets/186236df-e288-4747-bf21-de0257828c56" />

$$Origin Object Headers = Mark Word 8 byte + Klass Pointer 4 byte = 12 byte$$
$$Compact Object Headers = Mark Word + Klass Pointer = 8 byte$$

##### 참고
- https://openjdk.org/jeps/519
- https://openjdk.org/jeps/450

### 그럼 코틀린은 왜 사용하지 않으셨나요?
Kotlin이 Null Safety나 코루틴처럼 개발 생산성을 높여주는 기능을 제공한다는 점은 장점이라고 생각
기술을 선택할 때는 언어 자체의 편의성뿐만 아니라 **팀의 숙련도**까지 함께 고려해야 한다.
또한 최근 Java도 Virtual Thread를 통해 기존의 동기식 프로그래밍 모델을 유지하면서도 많은 동시 요청을 효율적으로 처리할 수 있도록 발전하고 있다.

따라서 Kotlin이 언어적인 편의성에서는 장점이 있다고 생각하지만, **언어 자체의 우열보다는 서비스의 요구사항과 팀의 기술 스택, 생태계와 장기적인 운영 비용까지 고려했을 때 저희 상황에서는 Java가 더 적합한 선택이라고 판단했다.**

## 빌드: Gradle

#### Maven과 Gradle

| 항목 | Maven | Gradle | 우리 서비스에서의 판단 |
|---|---|---|---|
| 스크립트 자유도 | POM XML 중심의 선언형 설정 | Groovy/Kotlin 기반 DSL과 코드 사용 | 빌드 조건과 규칙이 늘어날 때 Gradle의 표현력을 활용한다. |
| 빌드 흐름 커스터마이징 | Lifecycle, Profile, Plugin 설정이 필요 | Task를 조합하고 Script 로직을 직접 작성 | 주문·결제·검색의 검증 흐름을 필요한 만큼 구성한다. |
| 새로운 작업 추가 | 보통 Plugin Goal이나 custom Plugin을 작성 | Task와 Plugin으로 추가 | 반복되는 작업을 공통 Task로 만들 수 있다. |
| 조건부 분기 | Profile과 property를 조합 | Script에서 if·when을 사용 | 환경별 테스트·패키징 조건을 코드로 읽을 수 있게 작성한다. |
| 빌드 성능 | lifecycle과 Plugin 구현에 따라 달라짐 | Build Cache, Incremental Build, Daemon 등을 조합 | “항상 몇 배”가 아니라 동일 조건의 결과로 판단한다. |
| 유지보수성 | 표준 구조와 기존 Plugin 자산이 있으면 예측 가능 | 자유도가 큰 만큼 팀 규칙이 필요 | Kotlin DSL과 간단한 공통 규칙으로 자유도를 관리한다. |

##### 성능 비교

<img width="100%" src="https://github.com/user-attachments/assets/8669c92b-b557-4cce-bceb-31e020171415" />

| 실험 | workload와 방법 | Gradle이 Maven보다 빠르게 측정된 결과 |
|---|---|---|
| Java Library | Apache Commons Lang을 Maven과 Gradle로 변환 | 테스트 약 1.7배, Build Cache 사용 시 build 최대 30배 |
| Small Multi-Module | 10개 module, module당 main 50개·test 50개 source file | Clean 2~3배, Incremental 약 7배, Cache 최대 14배 |
| Medium Multi-Module | 100개 module, module당 main 100개·test 100개 source file | Clean 4~5배, Incremental 약 40배, Cache 최대 13배 |
| Large Multi-Module | 500개 module, module당 main 100개·test 100개 source file | Clean 3~10배, Incremental 약 85배, Cache 최대 13배 |
| Large Monolithic | main 50,000개·test 50,000개 source file | Clean 2~3배, Incremental 약 7배, Cache 최대 3배 |

###### 2018년형 MacBook Pro(2.9GHz Intel Core i9, 32GB RAM, SSD, macOS Mojave)에서 10회 실행의 평균을 사용했습니다.

#### Groovy DSL
##### 개념 요약
- Groovy DSL은 기존 Gradle 기본 스크립트인 build.gradle을 작성하는 방식이다.
- Groovy 기반이라 문법이 느슨하고 자유롭고, 함수와 DSL 스타일을 자연스럽게 표현할 수 있다.
- Java와 유사한 문법 구조와 Java 호환성을 갖기 때문에 Java 개발자가 접근하기 쉽다.
- 동적 문법의 특성상 IDE 자동완성과 오류 위치 확인이 제한될 수 있다.
- 새로 작성하는 Build Script에서는 Kotlin DSL과의 유지보수 비용을 비교한다.

##### 참고
- [Gradle Build File Basics](https://docs.gradle.org/current/userguide/build_file_basics.html)
- [Gradle Groovy DSL Primer](https://docs.gradle.org/current/userguide/groovy_dsl.html)

#### Kotlin DSL
##### 개념 요약
- Kotlin DSL은 .kts 확장자를 사용하는 Build Script 작성 방식이며, 파일 이름은 build.gradle.kts이다.
- Kotlin 문법을 사용하므로 정적 타입 검사를 받을 수 있고, IntelliJ에서 자동완성과 에러 하이라이트를 활용할 수 있다.
- 초기 설정은 Groovy DSL보다 엄격하고 익혀야 할 Kotlin 문법이 있어 학습 비용이 생길 수 있다.
- Kotlin DSL을 사용한다고 애플리케이션 코드까지 Kotlin으로 작성해야 하는 것은 아니다. Java + Spring Boot와 함께 사용할 수 있다.
- 여러 개발자가 빌드 스크립트를 자주 수정하는 환경에서 오타를 빨리 발견하고, 옵션을 자동완성으로 찾을 수 있다.
- 회원·도서·주문·결제 모듈이 늘어날 때 Build Script를 IDE에서 탐색하고 수정하기 쉽게 유지한다.

##### 참고

- [Gradle General Best Practices: Use Kotlin DSL](https://docs.gradle.org/current/userguide/best_practices_general.html)
- [Gradle Kotlin DSL Reference](https://docs.gradle.org/current/kotlin-dsl/index.html)
- [Migrating from Groovy to Kotlin DSL](https://docs.gradle.org/current/userguide/migrating_from_groovy_to_kotlin_dsl.html)

#### Plugin

##### 개념 요약

- Plugin은 빌드에 자주 사용하는 기능과 Task를 추가하는 확장 단위이다.
- 예를 들어 Java Plugin을 적용하면 Java compile, dependency, test와 관련된 빌드 작업을 구성할 수 있다.
- “Task들의 묶음”이라는 스크립트 표현은 이해를 돕는 비유이고, Plugin은 Task 외에도 빌드 설정과 모델을 추가할 수 있다.

- Java Plugin으로 Java 소스의 compile과 test 흐름을 만들고, Spring Boot Plugin으로 실행 가능한 Archive를 만든다.
- 같은 설정을 Java 파일마다 반복하지 않고 Plugin을 한 번 적용해 공통 빌드 작업을 사용한다.

##### 참고

- [Gradle Plugin Basics](https://docs.gradle.org/current/userguide/plugin_basics.html)
- [Spring Boot Gradle Plugin](https://docs.spring.io/spring-boot/gradle-plugin/)

#### Dependency Configuration

##### 개념 요약

- 프로젝트에서 사용하는 라이브러리와 패키지를 dependency라고 한다. Project별로 어떤 dependency를 갖는지 Build Script에 적어야 한다.
- implementation과 testImplementation 같은 Dependency Configuration은 라이브러리를 추가하는 “시점”이 아니라, 어느 classpath에서 어떤 범위로 사용하는지를 나타낸다.
- implementation은 애플리케이션과 라이브러리의 일반적인 compile/runtime 사용에, testImplementation은 테스트 코드에 사용하는 예이다.

- Spring Boot starter는 implementation에, 테스트 도구는 testImplementation에 선언해 의존성 사용 범위를 구분한다.
- 주문·결제·검색 모듈이 실제로 사용하는 dependency만 각 Project에 적어 불필요한 라이브러리와 관리 범위를 줄인다.

##### 참고

- [Gradle Dependency Configurations](https://docs.gradle.org/current/userguide/dependency_configurations.html)
- [Declaring Dependencies](https://docs.gradle.org/current/userguide/declaring_dependencies_basics.html)

#### Build Cache

##### 개념 요약

- Build Cache는 한 번 빌드한 Task 결과물을 저장해 같은 결과가 필요할 때 다시 사용하는 기능이다.
- 반복해서 빌드할 때 이미 만든 결과를 재사용하므로 전체 작업을 다시 수행하는 시간을 줄일 수 있다.
- Build Cache 결과를 사용했다는 표시는 UP-TO-DATE와 같지 않다. Cache에서 결과를 복원하면 FROM-CACHE로 표시된다.

##### 참고

- [Gradle Build Cache](https://docs.gradle.org/current/userguide/build_cache.html)
- [Build Cache Performance](https://docs.gradle.org/current/userguide/build_cache_performance.html)

#### Incremental Build

##### 개념 요약

- Incremental Build는 마지막 Build 호출 이후 변경된 부분을 중심으로 다시 빌드하는 방식이다.
- 변경되지 않은 부분은 다시 처리하지 않고, 필요한 부분만 추가로 빌드한다.
- 도서·검색 코드나 주문 정책을 조금 수정했을 때 변경된 부분의 compile과 test만 다시 수행되는지 확인한다.
- 실제 효과는 변경 파일 수, module 경계, Plugin 지원에 따라 달라지므로 우리 서비스에서 측정한다.

##### 참고

- [Incremental Builds](https://docs.gradle.org/current/userguide/incremental_build.html)
- [Java Plugin: Incremental Compilation](https://docs.gradle.org/current/userguide/java_plugin.html)

#### Up-to-date Check

##### 개념 요약

- Task의 input과 output이 변경되지 않았을 때 Gradle은 UP-TO-DATE를 표시하고 해당 Task를 실행하지 않는다.
- Build Cache에서 Task 결과를 복원하면 FROM-CACHE가 표시된다.
- 소스 변경 없이 회원·도서 모듈의 check를 다시 실행할 때 이미 유효한 Task를 건너뛰는지 확인한다.
- 사용자 정의 Task를 추가할 때 input과 output을 정확히 선언해 필요한 검증이 빠지지 않게 한다.

##### 참고

- [Task Outcomes](https://docs.gradle.org/current/userguide/gradle_optimizations.html)
- [Incremental Builds](https://docs.gradle.org/current/userguide/incremental_build.html)

#### Gradle Daemon

##### 개념 요약

- Gradle Daemon은 다음 빌드 작업을 위해 백그라운드에서 대기하는 장수 JVM 프로세스이다.
- 초기 빌드 이후에는 JVM을 다시 시작하고 초기화하는 비용을 줄일 수 있다.
- Daemon 프로세스에 Task 결과물을 저장해 재사용한다는 설명은 정확하지 않다. 결과물 저장과 재사용은 Build Cache의 역할이고, Daemon은 실행 환경과 일부 빌드 상태를 재사용한다.

- 개발자가 Java compile과 테스트를 반복하는 로컬 환경에서 Starting Gradle Daemon이 첫 실행에만 나타나는지 확인한다.
- CI가 매번 새 환경에서 실행되는지 프로세스를 재사용하는지에 따라 효과가 달라지므로 로컬과 CI를 나누어 측정한다.

##### 참고

- [Gradle Daemon](https://docs.gradle.org/current/userguide/gradle_daemon.html)
- [Gradle Build Cache](https://docs.gradle.org/current/userguide/build_cache.html)

## 프레임워크: Spring Boot

### 왜 스프링을 사용하나요?

- IoC/DI로 의존성 주입 → 유지보수가 쉬운 구조
- 관점 지향 프로그래밍(AOP) → 유지보수가 쉬운 구조

Spring Boot는 의존성과 설정 정보를 기반으로 웹 서버, DB 연결, JPA 같은 기본 구성을 자동으로 설정해주고, 내장 웹서버를 포함해 파일 하나만 실행해도 서버를 띄울 수 있다는 점이 편한 장점이 있다.
또한, 주문·결제·재고처럼 여러 DB 작업을 하나의 흐름으로 묶어야 하는 경우가 많기 때문에, @Transactional을 통해 여러 DB 작업의 성공과 실패를 일관되게 처리할 수 있다는 강점을 보고 선택했다.

## MySQL(InnoDB)

- MySQL은 SQL 서버이고 InnoDB는 행·인덱스 저장, 트랜잭션, 잠금, 제약조건, 복구를 담당하는 스토리지 엔진이다.
- InnoDB는 Transaction, MVCC, 현재 읽기, UNIQUE, Undo·Redo를 한 엔진에서 제공하므로 주문·재고·쿠폰의 기준 상태를 관리하기 적합하다.
- 기본 스토리지 엔진은 InnoDB지만 실제 테이블 엔진과 서버 설정은 운영 환경에서 확인한다.

##### 우리 서비스에 적용할 점

- `users`, `books`, `book_inventory`, `carts`, `cart_items`, `orders`, `order_items`, `payments`, `coupons`, `user_coupons`, `reviews`를 MySQL의 기준 데이터로 관리한다.

| 도서 이커머스 요구사항 | 핵심 기술 | 효과 |
|---|---|---|
| 주문·주문 상품·재고·쿠폰 저장 | Transaction / ACID | 일부만 반영되는 상태를 방지한다. |
| 동시 재고 차감 | Current Read / X Lock / 조건부 UPDATE | Overselling을 방지한다. |
| 결제 결과 반영 | Transaction / UNIQUE | 중복 이벤트와 상태 불일치를 줄인다. |
| 중복 주문 방지 | UNIQUE / Idempotency Key | 같은 요청의 중복 저장을 차단한다. |
| 장애 복구 | Undo / Redo / WAL | 롤백과 Crash Recovery를 지원한다. |
| 상품·주문 조회 | MVCC / Consistent Read | 조회와 쓰기의 불필요한 대기를 줄인다. |

##### 참고

- [InnoDB Storage Engine](https://dev.mysql.com/doc/refman/8.4/en/innodb-storage-engine.html)
- [InnoDB Transaction Model](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-model.html)
- [InnoDB and the ACID Model](https://dev.mysql.com/doc/refman/8.4/en/mysql-acid.html)

#### 트랜잭션
##### 개념 요약
- 트랜잭션은 여러 SQL을 하나의 논리적 작업으로 묶는다. 전부 성공하면 `COMMIT`, 하나라도 실패하면 `ROLLBACK`한다.
- Read Transaction은 조회를, Write Transaction은 `INSERT`·`UPDATE`·`DELETE`를 수행한다. `autocommit=1`이면 일반적으로 각 문장이 하나의 트랜잭션이 된다.

> [!NOTE]
> ACID는 Atomicity(전부 반영 또는 전부 취소), Consistency(제약조건과 규칙 유지), Isolation(동시 실행의 가시성·충돌 제어), Durability(커밋 결과의 장애 후 보존)를 뜻한다.

##### 우리 서비스에 적용할 점

- 주문은 `BEGIN → 재고 검증·차감 → orders → order_items → 쿠폰 사용 → COMMIT`으로 묶는다. 실패하면 전체를 롤백한다.
- 결제사 API는 DB 트랜잭션 밖의 동작이므로 주문 상태와 멱등 키로 별도 관리하고, 잠금을 잡은 채 외부 응답을 기다리지 않는다.

##### 참고

- [InnoDB Transaction Model](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-model.html)
- [Autocommit, Commit, and Rollback](https://dev.mysql.com/doc/refman/8.4/en/innodb-autocommit-commit-rollback.html)
- [InnoDB and the ACID Model](https://dev.mysql.com/doc/refman/8.4/en/mysql-acid.html)

#### Isolation Level

| 격리 수준 | Dirty | Non-Repeatable | Phantom |
|---|---|---|---|
| `READ UNCOMMITTED` | 허용 | 허용 | 허용 |
| `READ COMMITTED` | 방지 | 허용 | 허용 |
| `REPEATABLE READ` | 방지 | 방지 | 허용 가능 |
| `SERIALIZABLE` | 방지 | 방지 | 방지 |
###### Dirty Read는 미커밋 값을 읽는 현상, Non-Repeatable Read는 같은 행의 값이 달라지는 현상, Phantom Read는 같은 조건의 결과 행이 달라지는 현상입니다.

##### 우리 서비스에 적용할 점

- MySQL InnoDB 기본값은 `REPEATABLE READ`이다. 일반 `SELECT`는 스냅샷을, `FOR UPDATE`는 현재 값을 읽는다.
- RR의 잠금 읽기는 Next-Key/Gap Lock으로 검색 범위의 Phantom 삽입을 막을 수 있다. RC에서는 일반 검색의 Gap Lock이 줄어들지만 Non-Repeatable Read를 허용하므로 재고·쿠폰을 다시 조건 검증한다.

##### 참고

- [InnoDB Transaction Isolation Levels](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html)
- [Consistent Nonlocking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html)
- [InnoDB Locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)

#### Undo Log / Redo Log
##### 개념 요약
- **Undo Log**는 변경 전 상태를 보관해 `ROLLBACK`과 MVCC의 과거 버전 재구성에 사용한다.
- **Redo Log**는 Data Page에 반영되지 않은 변경을 장애 후 다시 적용해 Crash Recovery와 Durability를 지원한다. Undo와 Redo는 서로 대체하지 않는다.
- 오래 열린 Read View가 있으면 필요한 Undo Version을 바로 정리하지 못해 Undo History가 증가할 수 있다.

> [!NOTE]
> Write-Ahead Logging(WAL)은 Data Page보다 복구 로그를 먼저 영속화하는 원칙이다. InnoDB는 Redo Log를 먼저 기록하고 Data Page를 나중에 플러시할 수 있으므로, `COMMIT`이 모든 Data Page의 즉시 디스크 기록을 뜻하지는 않는다. 내구성 수준은 `innodb_flush_log_at_trx_commit`을 확인한다.

##### 우리 서비스에 적용할 점

- 주문·주문 상품·재고·쿠폰을 한 트랜잭션으로 변경하면 애플리케이션 오류 시 Undo로 되돌릴 수 있고, 서버 장애 시 Redo와 Undo로 복구한다.
- 주문 트랜잭션은 필요한 작업만 포함해 Undo History와 잠금 유지 시간을 줄인다.

##### 참고

- [InnoDB Undo Logs](https://dev.mysql.com/doc/refman/8.4/en/innodb-undo-logs.html)
- [InnoDB Redo Log](https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html)
- [InnoDB Recovery](https://dev.mysql.com/doc/refman/8.4/en/innodb-recovery.html)

#### MVCC
##### 개념 요약
- MVCC는 변경 전 버전을 유지해 일반 읽기가 쓰기 잠금과 항상 충돌하지 않도록 한다.
- InnoDB 레코드의 `DB_TRX_ID`는 마지막 변경 트랜잭션을, `DB_ROLL_PTR`는 Undo Chain을 가리킨다.

##### 우리 서비스에 적용할 점

- 상품 상세·도서 목록·주문 이력은 일반 조회로 읽고, 재고 변경은 현재 읽기나 조건부 UPDATE로 처리한다.

##### 참고

- [InnoDB Multi-Versioning](https://dev.mysql.com/doc/refman/8.4/en/innodb-multi-versioning.html)
- [Consistent Nonlocking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html)

#### Read View
##### 개념 요약
- Read View는 과거 행의 복사본이 아니라 버전 가시성을 판단하는 경계와 활성 트랜잭션 목록이다.
- 자신의 변경은 보이고, 스냅샷 전에 완료된 트랜잭션은 보인다. 생성 시점에 `m_ids`에 있던 활성 트랜잭션과 스냅샷 이후 시작한 트랜잭션의 변경은 보이지 않는다.

##### 우리 서비스에 적용할 점

- 상품·주문 조회의 일관성이 필요하면 RR 스냅샷을 사용하고, 주문 직전 재고 판단은 Read View가 아닌 `FOR UPDATE` 또는 조건부 UPDATE로 수행한다.

##### 참고

- [InnoDB Multi-Versioning](https://dev.mysql.com/doc/refman/8.4/en/innodb-multi-versioning.html)
- [MySQL 8.0.45 ReadView Class Reference](https://dev.mysql.com/doc/dev/mysql-server/8.0.45/classReadView.html)
- [InnoDB Transaction Isolation Levels](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-isolation-levels.html)

#### Consistent Read / Locking Read
##### 개념 요약
- Consistent Read(Snapshot Read)는 일반 `SELECT`가 Read View를 사용해 읽는 방식이다. RR은 트랜잭션 첫 스냅샷을 재사용하고 RC는 문장마다 새 스냅샷을 만든다.
- Locking Read(Current Read)는 `SELECT ... FOR UPDATE` 또는 `FOR SHARE`로 현재 버전을 읽고 각각 X 또는 S 잠금을 설정한다.

| 구분 | 일반 `SELECT` | `FOR UPDATE` / `FOR SHARE` |
|---|---|---|
| Version | 스냅샷 | 현재 버전 |
| Read View | 사용 | 해당 행 판정에는 사용하지 않음 |
| 잠금 | 일반적으로 없음 | X 또는 S |
| 동시 요청 | 쓰기 잠금과 보통 대기하지 않음 | 충돌 시 대기 |

##### 우리 서비스에 적용할 점

- 도서 목록·상세·리뷰 조회는 일반 `SELECT`로 처리하고, 주문 직전 재고는 `SELECT ... FOR UPDATE`로 다시 확인한다.

##### 참고

- [Consistent Nonlocking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html)
- [InnoDB Locking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)
- [Locks Set by Different SQL Statements](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)

#### Shared Lock / Exclusive Lock
##### 개념 요약
- Shared Lock(S)은 공유 읽기를 허용하고, **Exclusive Lock(X)**은 변경·삭제를 위해 다른 S/X와 충돌한다. S/X는 잠금 Mode이며 Record·Gap·Next-Key는 잠금 범위이다.

| 기존 잠금 | 요청 S | 요청 X |
|---|---|---|
| S | 허용 | 대기 |
| X | 대기 | 대기 |

##### 우리 서비스에 적용할 점

- 주문 재고는 S 잠금 후 승격하기보다 처음부터 `FOR UPDATE`로 X 잠금을 요청한다. 주문 취소의 재고 복구와 상태 변경도 X 잠금 대상이다.

##### 참고

- [InnoDB Locking: Shared, Exclusive, and Intention Locks](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)
- [InnoDB Locking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)

#### Record Lock (Row Lock) / Gap Lock / Next-Key Lock
##### 개념 요약
- InnoDB 공식 용어인 **Record Lock**은 논리적 행이 아니라 인덱스 레코드 하나를 잠근다. 일반적으로 말하는 Row Lock은 이 의미로 사용한다.
- **Gap Lock**은 인덱스 레코드 사이의 삽입 공간을 막는다. Gap S/X는 표기가 달라도 서로 충돌하지 않는 억제 잠금이며, INSERT는 Insert Intention Lock을 요청한다.
- **Next-Key Lock**은 Record Lock과 바로 앞 Gap Lock을 합친 범위이다. 값 20이면 `(10, 20]`을 보호하고 Phantom 삽입을 막을 수 있다.

<img width="100%" src="https://github.com/user-attachments/assets/0d162adb-4760-4598-bd0e-de1424bad845" />

- Record·Gap·Next-Key는 범위이고 S·X는 Mode이다. RR의 범위 검색은 Next-Key를 사용할 수 있지만, UNIQUE 전체 컬럼의 등가 검색은 보통 일치 레코드만 잠근다. RC의 일반 검색은 Gap Lock이 줄어든다.

##### 우리 서비스에 적용할 점

- 도서 조회는 일반 읽기로 처리하고, 주문 재고는 `book_inventory(book_id)`의 고유 인덱스로 한 행을 잠근다. 여러 도서는 `book_id` 순서로 잠가 범위와 대기를 줄인다.
- 잠금 범위는 `EXPLAIN`과 `performance_schema.data_locks`로 확인한다.

##### 참고

- [InnoDB Locking](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)
- [Locks Set by Different SQL Statements](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)
- [Performance Schema data_locks Table](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-locks-table.html)

#### Deadlock
##### 개념 요약
- Deadlock은 서로의 잠금을 기다리는 교착 상태를 의미한다. InnoDB는 Victim 트랜잭션 하나를 롤백해 나머지를 진행시킨다.
- 잠금 순서를 통일하면 가능성을 줄일 수 있지만 제거할 수는 없다. 실패한 트랜잭션 전체를 새 트랜잭션으로 재시도한다.

##### 참고

- [Deadlocks in InnoDB](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks.html)
- [How to Minimize and Handle Deadlocks](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html)
- [InnoDB Error Handling](https://dev.mysql.com/doc/refman/8.4/en/innodb-error-handling.html)

#### UNIQUE Constraint
##### 개념 요약
- UNIQUE는 검색 최적화가 아니라 중복을 DB가 원자적으로 거부하는 무결성 규칙이다. `SELECT → 없음 → INSERT`만으로는 동시 요청의 경쟁 구간을 막을 수 없다.
- 중복 INSERT는 Duplicate Key 오류가 되므로 애플리케이션이 기존 결과 조회나 Conflict 응답으로 변환해야 한다. UPSERT는 행을 갱신할 수 있으므로 멱등성과 같은 개념이 아니다.

##### 우리 서비스에 적용할 점

- `UNIQUE(user_id, idempotency_key)`로 중복 주문을 막고, 같은 키·같은 요청에는 기존 주문 결과를 반환한다. 같은 키·다른 본문은 Conflict로 처리한다.
- 리뷰를 주문 상품당 한 번만 허용하면 `UNIQUE(order_item_id)`를 둔다. 작성자 검증은 별도로 수행한다.

##### 참고

- [CREATE TABLE: UNIQUE Constraints](https://dev.mysql.com/doc/refman/8.4/en/create-table.html)
- [Locks Set by Different SQL Statements](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)
- [INSERT ... ON DUPLICATE KEY UPDATE](https://dev.mysql.com/doc/refman/8.4/en/insert-on-duplicate.html)

#### 조건부 UPDATE
##### 개념 요약
- 조건부 UPDATE는 조건 검사와 변경을 한 SQL에서 수행해 읽기와 쓰기 사이의 경쟁 구간을 줄인다.

##### 우리 서비스에 적용할 점

```sql
UPDATE book_inventory
SET stock = stock - :quantity
WHERE book_id = :bookId AND stock >= :quantity;
```

- affected rows가 1이면 재고 차감 성공으로 처리한다. 쿠폰은 `used_at IS NULL` 조건으로 갱신하고 affected rows를 확인한다.

##### 참고

- [UPDATE Statement](https://dev.mysql.com/doc/refman/8.4/en/update.html)
- [InnoDB Locking Reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)

## 인메모리DB: Redis

### 인메모리 DB는 왜 Redis를 쓰나요?

Refresh Token은 유효기간이 명확하고 로그아웃 시 즉시 무효화해야 하며 영구 보관할 필요가 없는 데이터이기 때문에 관계형 DB보다 TTL과 빠른 조회를 제공하는 Redis가 적합하다고 판단했다. 핵심 비즈니스 데이터는 MySQL에 저장하고, Redis는 일시적인 상태 데이터 관리에 한정해 사용한다. 추후 인기 도서나 추천 결과처럼 반복 조회가 많은 데이터에서 실제 병목이 확인되면 캐시 용도로 확장할 예정이다.

## 동기/비동기

### 동기/비동기 처리는 어떻게 하실 건가요?

사용자에게 즉시 결과가 필요한 주문·결제·조회는 동기로 처리하고, 알림이나 로그처럼 응답 이후 처리해도 되는 작업은 비동기로 분리할 계획이다. 현재는 비동기 작업의 수와 처리량이 많지 않아 MQ를 도입하지 않고, 향후 주문 완료 이벤트 하나에 알림·포인트·추천이 붙는 V2에서 RabbitMQ 도입을 검토할 예정이다.

### RabbitMQ와 kafka의 차이는 뭔가요?

RabbitMQ는 메시지를 특정 작업자에게 안정적으로 전달하는 작업 큐에 강하고, Kafka는 이벤트를 로그처럼 저장해서 여러 Consumer가 독립적으로 읽는 이벤트 스트리밍에 강하다. 현재 프로젝트의 비동기 요구는 알림이나 후속 작업 중심이라 Kafka보다는 RabbitMQ가 더 적합하다고 판단했다.
