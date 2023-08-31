#coding: utf-8
import math
import multiprocessing
import time
import threading
from collections import deque
from typing import Optional

from .ipc_shm_utils import SHM_manager,SHM_woker
import pickle
from ..utils import logger


class SHM_process_worker(SHM_woker):
    def __init__(self,*args,**kwargs):
        super(SHM_process_worker,self).__init__(*args,**kwargs)

    #Process begin trigger this func
    def run_begin(self):
        raise NotImplementedError

    # Process end trigger this func
    def run_end(self):
        raise NotImplementedError

    #any data put will trigger this func
    def run_once(self,request_data):
        raise NotImplementedError



class IPC_shm:
    def __init__(self,
                 CLS_worker,
                 worker_args: tuple,
                 worker_num: int,
                 manager_num: int,
                 group_name,
                 evt_quit=multiprocessing.Manager().Event(),
                 shm_size=1 * 1024 * 1024,
                 queue_size=20,
                 is_log_time=False,
                 daemon=False
                 ):
        self.__manager_lst = []
        self.__woker_lst = []
        self.__signal_list = []
        self.__shm_name_list = []

        self.__input_queue = None
        self.__output_queue = None


        self.request_id = 0
        self.pending_request = {}
        self.pending_response = {}

        self.locker = threading.Lock()

        assert isinstance(worker_args, tuple)
        self.__input_queue = multiprocessing.Manager().Queue(queue_size)
        self.__output_queue = multiprocessing.Manager().Queue(queue_size)

        semaphore = multiprocessing.Manager().Semaphore(worker_num)

        for i in range(worker_num):
            shm_name = '{}_jid_{}'.format(group_name, i)
            worker = CLS_worker(
                *worker_args,
                evt_quit,
                semaphore,
                shm_name,
                shm_size,
                is_log_time=is_log_time,
                idx=i,
                group_name=group_name,
                daemon=daemon)
            self.__signal_list.append(worker.get_signal())
            self.__shm_name_list.append(shm_name)
            self.__woker_lst.append(worker)

        semaphore = multiprocessing.Manager().Semaphore(manager_num)
        for i in range(manager_num):
            manager = SHM_manager(evt_quit,
                                  self.__signal_list,
                                  semaphore,
                                  self.__shm_name_list,
                                  self.__input_queue,
                                  self.__output_queue,
                                  is_log_time=is_log_time,
                                  idx=i)
            self.__manager_lst.append(manager)
        self.__last_t = time.time()
    def start(self):
        for w in self.__woker_lst:
            w.start()
        for w in self.__manager_lst:
            w.start()

    def put(self,data):
        self.locker.acquire()
        self.request_id += 1
        request_id = self.request_id
        self.pending_request[request_id] = time.time()
        self.__input_queue.put((request_id,pickle.dumps(data)))
        self.locker.release()
        return request_id


    def _check_and_clean(self):
        c_t = time.time()
        if math.floor((c_t - self.__last_t) / 600) > 0:
            self.__last_t = c_t
            invalid = set({rid for rid, t in self.pending_request.items() if math.floor((c_t - t) / 3600) > 0})
            logger.debug('remove {}'.format(str(list(invalid))))
            for rid in invalid:
                self.pending_request.pop(rid)
            invalid = set({rid for rid, t in self.pending_response.items() if math.floor((c_t - t["time"]) / 3600) > 0})
            for rid in invalid:
                self.pending_response.pop(rid)

    def _get_private(self, request_id, request_seq_id=None):
        response = None
        is_end = False
        timeout = 0.005
        while not is_end:
            with self.locker:
                if request_id in self.pending_request:
                    up_time = time.time()
                    self.pending_request[request_id] = up_time
                    reps = self.pending_response.get(request_id, None)
                    if reps is not None:
                        reps["time"] = up_time
                        rep: Optional[deque] = reps["data"]
                        item_size = len(rep)
                        if item_size > 0:
                            if request_seq_id is None:
                                (seq_id, response) = rep.popleft()
                                is_end = True
                            else:
                                for i, rep_sub in enumerate(rep):
                                    if rep_sub[0] == request_seq_id:
                                        (seq_id, response) = rep_sub
                                        rep.remove(rep_sub)
                                        is_end = True
                                        break

                        if len(rep) == 0:
                            self.pending_response.pop(request_id)

                    if not is_end:
                        is_empty = False
                        try:
                            item = self.__output_queue.get(block=False, timeout=timeout)
                        except:
                            item = None
                            is_empty = True
                        if not is_empty:
                            r_id, w_id, seq_id, response = item
                            if r_id != request_id or (request_seq_id is not None and request_seq_id != seq_id):
                                if r_id in self.pending_response:
                                    rep: Optional[deque] = self.pending_response[r_id]["data"]
                                    for i, node in enumerate(rep):
                                        if seq_id > node[0]:
                                            rep.insert(i + 1, (seq_id, response))
                                            break
                                else:
                                    self.pending_response[r_id] = {
                                        "time": time.time(),
                                        "data": deque([(seq_id, response)]),
                                        "last_seq": seq_id - 1,
                                    }
                            else:
                                is_end = True
                else:
                    logger.error('bad request_id {}'.format(request_id))
                    is_end = True

                if is_end:
                    self._check_and_clean()
                if is_end:
                    break
        return response

    def join(self):
        for p in self.__manager_lst:
            p.join()
        for p in self.__woker_lst:
            p.join()

    def terminate(self):
        for p in self.__woker_lst + self.__manager_lst:
            try:
                p.release()
            except Exception as e:
                pass
            p.terminate()

    @property
    def manager_process_list(self):
        return self.__manager_lst

    @property
    def woker_process_list(self):
        return self.__woker_lst