"""Kafka carries wake-up records for the PostgreSQL job schedulers."""
import asyncio
import logging
from time import monotonic

from avatar.core.execution import finish_on_cancel

logger = logging.getLogger(__name__)


class KafkaWakeups:
    def __init__(self, bootstrap, topic, topics, wake, set_ready):
        from confluent_kafka import Consumer, Producer

        settings = {'bootstrap.servers': bootstrap, 'log_level': 0}
        self.producer = Producer({
            **settings, 'enable.idempotence': True, 'linger.ms': 5,
            'delivery.timeout.ms': 3000, 'queue.buffering.max.messages': 2048,
        })
        self.consumer = Consumer({
            **settings, 'group.id': topic, 'auto.offset.reset': 'earliest',
            'enable.auto.offset.store': False, 'allow.auto.create.topics': True,
        })
        self.loop = asyncio.get_running_loop()
        self.topic, self.topics = topic, topics
        self.wake, self.set_ready = wake, set_ready
        self.consumer.subscribe([self.topic])

    def publish(self, key):
        from confluent_kafka import KafkaException

        ack = self.loop.create_future()

        def complete(error):
            if error is not None:
                self.set_ready(False)
                self.wake(key)
            if not ack.done():
                ack.set_result(None)

        def delivered(error, _):
            self.loop.call_soon_threadsafe(complete, error)

        try:
            self.producer.produce(self.topic, key=key, value=key, on_delivery=delivered)
        except (KafkaException, BufferError) as error:
            complete(error)
        return ack

    async def run(self):
        from confluent_kafka import KafkaException

        next_check = 0.
        try:
            while True:
                try:
                    batch = await finish_on_cancel(asyncio.to_thread(self.consumer.consume, 64, .5))
                    self.producer.poll(0)
                    for message in batch:
                        if message.error():
                            self.set_ready(False)
                            continue
                        value = message.value()
                        if value and value in self.topics:
                            self.wake(value.decode('ascii'))
                        self.consumer.store_offsets(message=message)
                    if monotonic() >= next_check:
                        metadata = await finish_on_cancel(asyncio.to_thread(
                            self.consumer.list_topics, self.topic, 1.0))
                        topic = metadata.topics.get(self.topic)
                        ready = bool(self.consumer.assignment() and topic and not topic.error and any(
                            partition.leader >= 0 for partition in topic.partitions.values()))
                        self.set_ready(ready)
                        next_check = monotonic() + 3.
                except KafkaException:
                    self.set_ready(False)
                    logger.debug('Kafka wake-up consumer unavailable')
                    await asyncio.sleep(.5)
        finally:
            self.set_ready(False)
            await asyncio.to_thread(self.consumer.close)
            await asyncio.to_thread(self.producer.flush, 1.)
