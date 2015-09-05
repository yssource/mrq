from .redishelpers import redis_zaddbyscore, redis_zpopbyscore, redis_lpopsafe
from .redishelpers import redis_group_command
import time
from bson import ObjectId
from . import context
from . import job as jobmodule


class Queue(object):
    """ A Queue for Jobs. """

    is_raw = False
    is_timed = False
    is_sorted = False
    is_set = False
    is_reverse = False

    use_large_ids = False

    # This is a mutable type so it is shared by all instances
    # of Queue in the current process
    known_queues = set([])

    def __init__(self, queue_id):
        if isinstance(queue_id, Queue):
            self.id = queue_id.id  # TODO use __new__?
            self.is_reverse = queue_id.is_reverse
        else:
            if queue_id[-8:] == "_reverse":
                self.is_reverse = True
                queue_id = queue_id[:-8]
            self.id = queue_id

        # Queue types are determined by their suffix.
        if "_raw" in self.id:
            self.is_raw = True

        if "_set" in self.id:
            self.is_set = True
            self.is_raw = True

        if "_timed" in self.id:
            self.is_timed = True
            self.is_sorted = True

        if "_sorted" in self.id:
            self.is_sorted = True

        self.use_large_ids = context.get_current_config()["use_large_job_ids"]

        # If this is the first time this process sees this queue, try to add it
        # on the shared redis set.
        if self.id not in self.known_queues:
            known_queues_key = "%s:known_queues" % context.get_current_config()["redis_prefix"]
            context.connections.redis.sadd(known_queues_key, self.id)
            self.known_queues.add(self.id)

    @property
    def redis_key(self):
        """ Returns the redis key used to store this queue. """
        return "%s:q:%s" % (context.get_current_config()["redis_prefix"], self.id)

    @classmethod
    def redis_key_started(cls):
        """ Returns the global redis key used to store started job ids """
        return "%s:s:started" % context.get_current_config()["redis_prefix"]

    def get_retry_queue(self):
        """ For raw queues, returns the name of the linked queue for job statuses
            other than "queued" """

        if not self.is_raw:
            return self.id

        return self.get_config().get("retry_queue") or "default"

    @classmethod
    def redis_known_queues(cls):
        """ Returns the global known_queues as stored in redis. """
        known_queues_key = "%s:known_queues" % context.get_current_config()["redis_prefix"]
        return context.connections.redis.smembers(known_queues_key)

    def get_config(self):
        """ Returns the specific configuration for this queue """

        return context.get_current_config().get("raw_queues", {}).get(self.id) or {}

    def serialize_job_ids(self, job_ids):
        """ Returns job_ids serialized for storage in Redis """
        if len(job_ids) == 0 or self.use_large_ids:
            return job_ids
        elif isinstance(job_ids[0], ObjectId):
            return [x.binary for x in job_ids]
        else:
            return [x.decode('hex') for x in job_ids]

    def unserialize_job_ids(self, job_ids):
        """ Unserialize job_ids stored in Redis """
        if len(job_ids) == 0 or self.use_large_ids:
            return job_ids
        else:
            return [x.encode('hex') for x in job_ids]

    def size(self):
        """ Returns the total number of jobs on the queue """

        # ZSET
        if self.is_sorted:
            return context.connections.redis.zcard(self.redis_key)
        # SET
        elif self.is_set:
            return context.connections.redis.scard(self.redis_key)
        # LIST
        else:
            return context.connections.redis.llen(self.redis_key)

    def count_jobs_to_dequeue(self):
        """ Returns the number of jobs that can be dequeued right now from the queue. """

        # timed ZSET
        if self.is_timed:
            return context.connections.redis.zcount(
                self.redis_key,
                "-inf",
                time.time())

        # In all other cases, it's the same as .size()
        else:
            return self.size()

    def list_job_ids(self, skip=0, limit=20):
        """ Returns a list of job ids on a queue """

        if self.is_raw:
            raise Exception("Can't list job ids from a raw queue")

        return self.unserialize_job_ids(self._get_queue_content(skip, limit))

    def list_raw_jobs(self, skip=0, limit=20):

        if not self.is_raw:
            raise Exception("Queue is not raw")

        return self._get_queue_content(skip, limit)

    def _get_queue_content(self, skip, limit):
        if self.is_sorted:
            return context.connections.redis.zrange(
                self.redis_key,
                skip,
                skip + limit - 1)
        # SET
        elif self.is_set:
            return context.connections.redis.srandmember(self.redis_key, limit)

        # LIST
        else:
            return context.connections.redis.lrange(
                self.redis_key,
                skip,
                skip + limit - 1)

    def get_sorted_graph(
            self,
            start=0,
            stop=100,
            slices=100,
            include_inf=False,
            exact=False):
        """ Returns a graph of the distribution of jobs in a sorted set """

        if not self.is_sorted:
            raise Exception("Not a sorted queue")

        with context.connections.redis.pipeline(transaction=exact) as pipe:
            interval = float(stop - start) / slices
            for i in range(0, slices):
                pipe.zcount(self.redis_key,
                            (start + i * interval),
                            "(%s" % (start + (i + 1) * interval))
            if include_inf:
                pipe.zcount(self.redis_key, stop, "+inf")
                pipe.zcount(self.redis_key, "-inf", "(%s" % start)
            data = pipe.execute()

        if include_inf:
            return data[-1:] + data[:-1]

        return data

    @classmethod
    def all_active(cls):
        """ List active queues, based on their lengths in Redis. """

        prefix = context.get_current_config()["redis_prefix"]
        queues = []
        for key in context.connections.redis.keys():
            if key.startswith(prefix):
                queues.append(Queue(key[len(prefix) + 3:]))

        return queues

    @classmethod
    def all_known(cls):
        """ List all previously known queues and their lengths in MongoDB """

        # Start with raw queues we know exist from the config
        queues = {x: 0 for x in context.get_current_config().get("raw_queues", {})}

        known_queues = cls.redis_known_queues()

        for q in known_queues:
            if q not in queues:
                queues[q] = context.connections.mongodb_jobs.mrq_jobs.count({
                    "queue": q,
                    "status": "queued"
                })

        return queues

    @classmethod
    def all(cls):
        """ List *all* queues in MongoDB via aggregation. Might be slow. """

        # Start with raw queues we know exist from the config
        queues = {x: 0 for x in context.get_current_config().get("raw_queues", {})}

        stats = list(context.connections.mongodb_jobs.mrq_jobs.aggregate([
            {"$match": {"status": "queued"}},
            {"$group": {"_id": "$queue", "jobs": {"$sum": 1}}}
        ]))

        queues.update({x["_id"]: x["jobs"] for x in stats})

        return queues

    def enqueue_job_ids(self, job_ids):
        """ Add Jobs to this queue, once they have been inserted in MongoDB. """

        if len(job_ids) == 0:
            return

        if self.is_raw:
            raise Exception("Can't queue regular jobs on a raw queue")

        # ZSET
        if self.is_sorted:

            if not isinstance(job_ids, dict) and self.is_timed:
                now = time.time()
                job_ids = {x: now for x in self.serialize_job_ids(job_ids)}
            else:

                serialized_job_ids = self.serialize_job_ids(job_ids.keys())
                values = job_ids.values()
                job_ids = {k: values[i] for i, k in enumerate(serialized_job_ids)}

            context.connections.redis.zadd(self.redis_key, **job_ids)

        # LIST
        else:
            context.connections.redis.rpush(self.redis_key, *self.serialize_job_ids(job_ids))

        context.metric("queues.%s.enqueued" % self.id, len(job_ids))
        context.metric("queues.all.enqueued", len(job_ids))

    def enqueue_raw_jobs(self, params_list):
        """ Add Jobs to this queue with raw parameters. They are not yet in MongoDB. """

        if not self.is_raw:
            raise Exception("Can't queue raw jobs in a regular queue")

        if len(params_list) == 0:
            return

        # ZSET
        if self.is_sorted:

            if not isinstance(params_list, dict) and self.is_timed:
                now = time.time()
                params_list = {x: now for x in params_list}

            context.connections.redis.zadd(self.redis_key, **params_list)

        # SET
        elif self.is_set:
            context.connections.redis.sadd(self.redis_key, *params_list)

        # LIST
        else:
            context.connections.redis.rpush(self.redis_key, *params_list)

        context.metric("queues.%s.enqueued" % self.id, len(params_list))
        context.metric("queues.all.enqueued", len(params_list))

    def remove_raw_jobs(self, params_list):
        """ Remove jobs from a raw queue with their raw params. """

        if not self.is_raw:
            raise Exception("Can't remove raw jobs in a regular queue")

        if len(params_list) == 0:
            return

        # ZSET
        if self.is_sorted:
            context.connections.redis.zrem(self.redis_key, *iter(params_list))

        # SET
        elif self.is_set:
            context.connections.redis.srem(self.redis_key, *params_list)

        else:
            # O(n)! Use with caution.
            for k in params_list:
                context.connections.redis.lrem(self.redis_key, 1, k)

        context.metric("queues.%s.removed" % self.id, len(params_list))
        context.metric("queues.all.removed", len(params_list))

    def empty(self):
        """ Empty a queue. """
        return context.connections.redis.delete(self.redis_key)

    def dequeue_jobs(self, max_jobs=1, job_class=None, worker=None):
        """ Fetch a maximum of max_jobs from this queue """

        if job_class is None:
            from .job import Job
            job_class = Job

        # Used in tests to simulate workers exiting abruptly
        simulate_zombie_jobs = context.get_current_config().get("simulate_zombie_jobs")

        jobs = []

        if self.is_raw:

            queue_config = self.get_config()

            statuses_no_storage = queue_config.get("statuses_no_storage")
            job_factory = queue_config.get("job_factory")
            if not job_factory:
                raise Exception("No job_factory configured for raw queue %s" % self.id)

            retry_queue = self.get_retry_queue()

            params = []

            # ZSET with times
            if self.is_timed:

                current_time = time.time()

                # When we have a pushback_seconds argument, we never pop items from
                # the queue, instead we push them back by an amount of time so
                # that they don't get dequeued again until
                # the task finishes.

                pushback_time = current_time + float(queue_config.get("pushback_seconds") or 0)
                if pushback_time > current_time:
                    params = redis_zaddbyscore()(
                        keys=[self.redis_key],
                        args=[
                            "-inf", current_time, 0, max_jobs, pushback_time
                        ])

                else:
                    params = redis_zpopbyscore()(
                        keys=[self.redis_key],
                        args=[
                            "-inf", current_time, 0, max_jobs
                        ])

            # ZSET
            elif self.is_sorted:

                # TODO Lua?
                with context.connections.redis.pipeline(transaction=True) as pipe:
                    pipe.zrange(self.redis_key, 0, max_jobs - 1)
                    pipe.zremrangebyrank(self.redis_key, 0, max_jobs - 1)
                    params = pipe.execute()[0]

            # SET
            elif self.is_set:
                params = redis_group_command("spop", max_jobs, self.redis_key)

            # LIST
            else:
                params = redis_group_command("lpop", max_jobs, self.redis_key)

            if len(params) == 0:
                return []

            # Caution, not having a pushback_time may result in lost jobs if the worker interrupts
            # before the mongo insert!
            if simulate_zombie_jobs:
                return []

            if worker:
                worker.status = "spawn"

            job_data = [job_factory(p) for p in params]
            for j in job_data:
                j["status"] = "started"
                j["queue"] = retry_queue
                if worker:
                    j["worker"] = worker.id

            jobs += job_class.insert(job_data, statuses_no_storage=statuses_no_storage)

        # Regular queue, in a LIST
        else:

            # TODO implement _timed and _sorted queues here.

            job_ids = redis_lpopsafe()(keys=[
                self.redis_key,
                Queue.redis_key_started()
            ], args=[
                max_jobs,
                time.time(),
                "0" if self.is_reverse else "1"
            ])

            if len(job_ids) == 0:
                return []

            # At this point, the job is in the redis started zset but not in Mongo yet.
            # It may become "zombie" if we interrupt here but we can recover it from
            # the started zset.
            if simulate_zombie_jobs:
                return []

            if worker:
                worker.status = "spawn"
                worker.idle_event.clear()

            jobs += [job_class(_job_id, queue=self.id, start=True)
                     for _job_id in self.unserialize_job_ids(job_ids) if _job_id]

            # Now that the jobs have been marked as started in Mongo, we can
            # remove them from the started queue.
            context.connections.redis.zrem(Queue.redis_key_started(), *job_ids)

        for job in jobs:
            context.metric("queues.%s.dequeued" % job.queue, 1)
        context.metric("queues.all.dequeued", len(jobs))

        return jobs


#
# Deprecated methods. Tagged for removal in 1.0.0
#

def send_raw_tasks(*args, **kwargs):
    return jobmodule.queue_raw_jobs(*args, **kwargs)


def send_task(path, params, **kwargs):
    return send_tasks(path, [params], **kwargs)[0]


def send_tasks(path, params_list, queue=None, sync=False, batch_size=1000):
    if sync:
        return [context.run_task(path, params) for params in params_list]

    return jobmodule.queue_jobs(path, params_list, queue=queue, batch_size=batch_size)
