# Messages, schemas and topics

- A field another service reads is a contract. Add, never rename or remove; keep
  the old field populated until every consumer has moved.
- Avro enum → string: change the TYPE only. Never rename a `*Enum` field —
  downstream readers use the name.
- A new or changed schema needs the schema registered for every environment's
  topic before the producer deploys; check the topic version in each helm values file.
- Trace the schema the consumer actually reads (its generated classes or
  registry subject) before editing the producer's copy.
