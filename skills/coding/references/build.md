# Building and testing his services

- Maven services: `mvn -f service/pom.xml clean test -Dtest=<Class>` — always
  `clean` (MapStruct generated mappers go stale otherwise). Checkstyle fails the
  build at `validate`, before any test runs.
- JDK 21. On a newer JDK Lombok silently skips annotation processing and you get
  a wall of false "cannot find symbol" errors — check `java -version` first.
- Output to a file, tail it: `... > /tmp/build.log 2>&1; tail -5 /tmp/build.log`.
- "Tests run: 0" is a failure: the filter matched nothing.
- Component tests need Docker; leave them to CI unless the task is about them.
