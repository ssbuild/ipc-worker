# @Time    : 2021/11/26 21:15
# @Author  : tk
# @FileName: zmq_utils.py
import json
import threading
import time
import typing
import zmq
from multiprocessing import Queue
from multiprocessing import Event,Process
import pickle
from datetime import datetime
from .ipc_utils_func import auto_bind
from ..utils import logger


class ZMQ_worker(Process):
    def __init__(self,identity,group_name,evt_quit,is_log_time,idx,daemon=False):
        super(ZMQ_worker,self).__init__(daemon=daemon)
        self.__identity = identity
        self._group_name = group_name
        self._evt_quit = evt_quit
        self._idx = idx
        self._is_log_time = is_log_time

        self.signal = Event()
        self.__is_closed = False


    def _set_addr(self,addr_sink,addr_pub):
        self._addr_sink = addr_sink
        self._addr_pub = addr_pub

    # Process begin trigger this func
    def run_begin(self):
        raise NotImplementedError

    # Process end trigger this func
    def run_end(self):
        raise NotImplementedError

    # any data put will trigger this func
    def run_once(self, request_data):
        raise NotImplementedError

    def __processinit__(self):
        self._context = zmq.Context()
        self._receiver = self._context.socket(zmq.SUB)
        self._receiver.setsockopt(zmq.SUBSCRIBE, self.__identity)
        # self._receiver.setsockopt(zmq.SUBSCRIBE, b'')

        # self._receiver.connect('tcp://{}:{}'.format(self._ip, self._port))
        self._receiver.connect(self._addr_pub)

        self._sender = self._context.socket(zmq.PUSH)
        self._sender.setsockopt(zmq.LINGER, 0)
        # self._sender.connect('tcp://{}:{}'.format(self._ip, self._port_out))
        self._sender.connect(self._addr_sink)

    def release(self):
        try:
            if not self.__is_closed:
                self.__is_closed = True
                self._receiver.close()
                self._context.term()
                self._sender.close()
        except Exception as e:
            ...

    def run(self):
        self.__processinit__()
        self.signal.set()
        self.run_begin()

        try:
            while not self._evt_quit.is_set():
                _,msg,b_request_id = self._receiver.recv_multipart()
                if self.__is_closed:
                    break
                msg_size = len(msg)
                request_data = pickle.loads(msg)
                start_t = datetime.now()
                XX = self.run_once(request_data)
                seq_id = 0
                if isinstance(XX, typing.Generator):
                    for X in XX:
                        seq_id += 1
                        X = pickle.dumps(X)
                        self._sender.send_multipart([b_request_id,int.to_bytes(self._idx,4,byteorder="little",signed=False),int.to_bytes(seq_id,4,byteorder="little",signed=False),X])
                else:
                    X = pickle.dumps(XX)
                    self._sender.send_multipart([b_request_id,int.to_bytes(self._idx,4,byteorder="little",signed=False),int.to_bytes(seq_id,4,byteorder="little",signed=False), X])

                if self._is_log_time:
                    deata = datetime.now() - start_t
                    micros = deata.seconds * 1000 + deata.microseconds / 1000
                    logger.debug('worker msg_size {} , runtime {}'.format(msg_size, micros))
        except Exception as e:
            print(e)

        self.run_end()
        self.release()




class ZMQ_sink(Process):
    def __init__(self,queue_size,group_name,evt_quit,daemon=False):
        super(ZMQ_sink,self).__init__(daemon=daemon)

        self.group_name = group_name
        self.__is_closed = False
        self.evt_quit = evt_quit

        self.queue = Queue(maxsize=queue_size)
        self.addr = None


    def wait_init(self):
        self.addr = self.queue.get()

    def get_queue(self) -> Queue:
        return self.queue

    def __processinit__(self):
        self.context = zmq.Context()
        self.receiver = self.context.socket(zmq.PULL)
        self.receiver.setsockopt(zmq.LINGER, 0)
        # self.receiver.bind('tcp://*:{}'.format(self.port_out))
        self.addr = auto_bind(self.receiver)
        logger.debug('group {} sink bind {}'.format(self.group_name,self.addr))
        self.queue.put(self.addr)

    def release(self):
        try:
            if not self.__is_closed:
                self.__is_closed = True
                self.receiver.close()
                self.queue.close()
                self.queue.join_thread()
                self.context.term()
        except Exception as e:
            ...



    def run(self):
        self.__processinit__()
        try:
            while not self.evt_quit.is_set():
                request_id,w_id,seq_id,response = self.receiver.recv_multipart()
                if self.__is_closed:
                    break
                r_id = int.from_bytes(request_id, byteorder='little', signed=False)
                w_id = int.from_bytes(w_id, byteorder='little', signed=False)
                seq_id = int.from_bytes(seq_id, byteorder='little', signed=False)
                self.queue.put((r_id,w_id,seq_id,response))
        except Exception as e:
            print(e)
        self.release()






class ZMQ_manager(Process):
    def __init__(self,idx,queue_size,group_name,evt_quit,daemon=False):
        super(ZMQ_manager, self).__init__(daemon=daemon)
        self.group_name = group_name
        self.request_id = 0
        self.idx = idx

        self.queue = Queue(queue_size)
        self.evt_quit = evt_quit
        self.locker = threading.Lock()
        self.addr = None
        self.__is_closed = False

    def wait_init(self):
        self.addr = self.queue.get()

    def put(self,identity,msg):
        self.locker.acquire()
        self.request_id += 1
        request_id = self.request_id
        self.locker.release()
        self.queue.put((request_id,identity,msg))
        return request_id

    def __processinit__(self):
        self.context = zmq.Context()
        self.sender = self.context.socket(zmq.PUB)
        self.sender.setsockopt(zmq.LINGER, 0)
        # self.sender.bind('tcp://*:{}'.format(self.port))
        self.addr = auto_bind(self.sender)
        self.queue.put(self.addr)

    def release(self):
        try:
            if not self.__is_closed:
                self.__is_closed = True
                self.queue.close()
                self.queue.join_thread()
                self.sender.close()
                self.context.term()
        except Exception as e:
            ...

    def run(self):
        self.__processinit__()
        logger.debug('group {} manager bind {}'.format(self.group_name,self.addr))
        try:
            while not self.evt_quit.is_set():
                request_id,identity,msg = self.queue.get()
                if self.__is_closed:
                    break
                self.sender.send_multipart([identity,msg, request_id.to_bytes(4,byteorder='little',signed=False)])
        except Exception as e:
            print(e)
        self.release()
