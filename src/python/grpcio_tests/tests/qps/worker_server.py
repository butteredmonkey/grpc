# Copyright 2016 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from concurrent import futures
import multiprocessing
import random
import threading
import time
import resource

import grpc

from src.proto.grpc.testing import benchmark_service_pb2_grpc
from src.proto.grpc.testing import control_pb2
from src.proto.grpc.testing import stats_pb2
from src.proto.grpc.testing import worker_service_pb2_grpc
from tests.qps import benchmark_client
from tests.qps import benchmark_server
from tests.qps import client_runner
from tests.qps import histogram
from tests.unit import resources
from tests.unit import test_common


class Snapshotter:

    def __init__(self, port):
        self._start_time = 0.0
        self._end_time = 0.0
        self._last_utime = 0.0
        self._utime = 0.0
        self._last_stime = 0.0
        self._stime = 0.0

        self._port = port
        self._cores = multiprocessing.cpu_count()
        self.snapshot()
        self.reset()

    def get_time_elapsed(self):
        return self._end_time - self._start_time

    def get_utime(self):
        return self._utime - self._last_utime

    def get_stime(self):
        return self._stime - self._last_stime

    def snapshot(self):
        self._end_time = time.time()

        usage = resource.getrusage(resource.RUSAGE_SELF)
        self._utime = usage.ru_utime
        self._stime = usage.ru_stime

    def reset(self):
        self._start_time = self._end_time
        self._last_utime = self._utime
        self._last_stime = self._stime

    def server_status(self):
        stats = stats_pb2.ServerStats(time_elapsed=self.get_time_elapsed(),
                                      time_user=self.get_utime(),
                                      time_system=self.get_stime())
        return control_pb2.ServerStatus(stats=stats,
                                        port=self._port,
                                        cores=self._cores)


class WorkerServer(worker_service_pb2_grpc.WorkerServiceServicer):
    """Python Worker Server implementation."""

    def __init__(self, server_port=None):
        self._quit_event = threading.Event()
        self._server_port = server_port
        self._snapshotter = None

    def RunServer(self, request_iterator, context):
        config = next(request_iterator).setup  #pylint: disable=stop-iteration-return
        server, port = self._create_server(config)
        self._snapshotter = Snapshotter(port)
        server.start()
        yield self._snapshotter.server_status()

        for request in request_iterator:
            self._snapshotter.snapshot()
            status = self._snapshotter.server_status()
            if request.mark.reset:
                self._snapshotter.reset()
            yield status
        server.stop(None)

    def _create_server(self, config):
        if config.async_server_threads == 0:
            # This is the default concurrent.futures thread pool size, but
            # None doesn't seem to work
            server_threads = multiprocessing.cpu_count() * 5
        else:
            server_threads = config.async_server_threads
        server = test_common.test_server(max_workers=server_threads)
        if config.server_type == control_pb2.ASYNC_SERVER:
            servicer = benchmark_server.BenchmarkServer()
            benchmark_service_pb2_grpc.add_BenchmarkServiceServicer_to_server(
                servicer, server)
        elif config.server_type == control_pb2.ASYNC_GENERIC_SERVER:
            resp_size = config.payload_config.bytebuf_params.resp_size
            servicer = benchmark_server.GenericBenchmarkServer(resp_size)
            method_implementations = {
                'StreamingCall':
                    grpc.stream_stream_rpc_method_handler(servicer.StreamingCall
                                                         ),
                'UnaryCall':
                    grpc.unary_unary_rpc_method_handler(servicer.UnaryCall),
            }
            handler = grpc.method_handlers_generic_handler(
                'grpc.testing.BenchmarkService', method_implementations)
            server.add_generic_rpc_handlers((handler,))
        else:
            raise Exception('Unsupported server type {}'.format(
                config.server_type))

        if self._server_port is not None and config.port == 0:
            server_port = self._server_port
        else:
            server_port = config.port

        if config.HasField('security_params'):  # Use SSL
            server_creds = grpc.ssl_server_credentials(
                ((resources.private_key(), resources.certificate_chain()),))
            port = server.add_secure_port('[::]:{}'.format(server_port),
                                          server_creds)
        else:
            port = server.add_insecure_port('[::]:{}'.format(server_port))

        return (server, port)

    def RunClient(self, request_iterator, context):
        config = next(request_iterator).setup  #pylint: disable=stop-iteration-return
        client_runners = []
        qps_data = histogram.Histogram(config.histogram_params.resolution,
                                       config.histogram_params.max_possible)
        start_time = time.time()

        # Create a client for each channel
        for i in range(config.client_channels):
            server = config.server_targets[i % len(config.server_targets)]
            runner = self._create_client_runner(server, config, qps_data)
            client_runners.append(runner)
            runner.start()

        end_time = time.time()
        yield self._get_client_status(start_time, end_time, qps_data)

        # Respond to stat requests
        for request in request_iterator:
            end_time = time.time()
            status = self._get_client_status(start_time, end_time, qps_data)
            if request.mark.reset:
                qps_data.reset()
                start_time = time.time()
            yield status

        # Cleanup the clients
        for runner in client_runners:
            runner.stop()

    def _get_client_status(self, start_time, end_time, qps_data):
        latencies = qps_data.get_data()
        end_time = time.time()
        elapsed_time = end_time - start_time
        stats = stats_pb2.ClientStats(latencies=latencies,
                                      time_elapsed=elapsed_time,
                                      time_user=elapsed_time,
                                      time_system=elapsed_time)
        return control_pb2.ClientStatus(stats=stats)

    def _create_client_runner(self, server, config, qps_data):
        no_ping_pong = False
        if config.client_type == control_pb2.SYNC_CLIENT:
            if config.rpc_type == control_pb2.UNARY:
                client = benchmark_client.UnarySyncBenchmarkClient(
                    server, config, qps_data)
            elif config.rpc_type == control_pb2.STREAMING:
                client = benchmark_client.StreamingSyncBenchmarkClient(
                    server, config, qps_data)
            elif config.rpc_type == control_pb2.STREAMING_FROM_SERVER:
                no_ping_pong = True
                client = benchmark_client.ServerStreamingSyncBenchmarkClient(
                    server, config, qps_data)
        elif config.client_type == control_pb2.ASYNC_CLIENT:
            if config.rpc_type == control_pb2.UNARY:
                client = benchmark_client.UnaryAsyncBenchmarkClient(
                    server, config, qps_data)
            else:
                raise Exception('Async streaming client not supported')
        else:
            raise Exception('Unsupported client type {}'.format(
                config.client_type))

        # In multi-channel tests, we split the load across all channels
        load_factor = float(config.client_channels)
        if config.load_params.WhichOneof('load') == 'closed_loop':
            runner = client_runner.ClosedLoopClientRunner(
                client, config.outstanding_rpcs_per_channel, no_ping_pong)
        else:  # Open loop Poisson
            alpha = config.load_params.poisson.offered_load / load_factor

            def poisson():
                while True:
                    yield random.expovariate(alpha)

            runner = client_runner.OpenLoopClientRunner(client, poisson())

        return runner

    def CoreCount(self, request, context):
        return control_pb2.CoreResponse(cores=multiprocessing.cpu_count())

    def QuitWorker(self, request, context):
        self._quit_event.set()
        return control_pb2.Void()

    def wait_for_quit(self):
        self._quit_event.wait()
