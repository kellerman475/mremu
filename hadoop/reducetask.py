# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

from comm import SHUFFLESERVER_PORT
from threading import Thread, Condition, Lock
import time
import sys
import traceback
import socket
import cPickle as pickle
from subprocess import Popen

TIMEOUT = 3600

IPERF_PORT = 7770

class ReduceTask(Thread):
    """
    A reduce task.
    """

    def __init__(self, task, control, maxParallelTransfer, queue):
        Thread.__init__(self)
        self.task = task
        self.name = "Thread-%s" % task.name
        self.control = control
        self.queue = queue
        self.copierControl = CopierControl(control)
        self.num_partitions = len(self.task.partitions)
        self.maxParallelTransfer = maxParallelTransfer

    def run(self):
        print "ReduceTask: starting (%s)" % self.task.name
        self.control.start_reduce_task(self.task)
    
        # Sleep to simulate the initial delay
        time.sleep(self.task.initialDelay)
        self.task.waitFinished = time.time()

        threads = []
        self.copierControl.cond.acquire()
        
        remaining_partitions = self.num_partitions 
        # Start first copier threads 
        for i in xrange(self.maxParallelTransfer):
            if remaining_partitions <= 0:
                break
            partition = self.queue.get(True)
            print "ReduceTask: copy partition - " + str(partition)
            copier = ReduceCopier(partition, self.copierControl)
            copier.start()
            threads.append(copier)
            #copier.join()
            remaining_partitions -= 1
            print "COPIEI %d PARTICOES DE %d NO TOTAL" % (i,self.num_partitions)

        start_time = time.time()
        while self.copierControl.finished_copier_threads < self.num_partitions and (time.time() - start_time < TIMEOUT):
            self.copierControl.cond.wait(3)
            free_slots = self.maxParallelTransfer - self.copierControl.active_copier_threads
            #if DEBUG is True:
            #    print "%s: active_copier_threads=%d, finished_copier_threads=%d, num_partitions=%d, remaining_partitions=%d, free_copier_slots=%d" % (self.task.name,
            #    self.copierControl.active_copier_threads, self.copierControl.finished_copier_threads,
            #    self.num_partitions, remaining_partitions, free_slots)
            #    #print "maxParallelTransfer=%d" % self.maxParallelTransfer
            for j in xrange(free_slots):
                if remaining_partitions <= 0:
                    break
                if self.queue.empty():
                    break
                
                self.copierControl.cond.release()
                partition = self.queue.get(True)
                self.copierControl.cond.acquire()
                
                print "Recebendo " + str(partition)
                copier = ReduceCopier(partition, self.copierControl)
                copier.start()
                threads.append(copier)
                #copier.join()
                remaining_partitions -= 1
                #self.queue.task_done()
                print "COPIEI %d PARTICOES DE %d NO TOTAL" % (self.num_partitions-remaining_partitions,self.num_partitions)

#        if DEBUG is True:
#            print "%s: active_copier_threads=%d, finished_copier_threads=%d, num_partitions=%d, remaining_partitions=%d, free_copier_slots=%d (end)" % (self.task.name,
#            self.copierControl.active_copier_threads, self.copierControl.finished_copier_threads,
#            self.num_partitions, remaining_partitions, free_slots)
#        
        self.copierControl.cond.release()

        # Wait for all of them to finish
        [x.join() for x in threads]

        time.sleep(self.task.mergingTime)        
        self.task.shuffleFinished = time.time()

        # Sleep to simulate the tasks exectution time
        time.sleep(self.task.sortingTime)
        self.task.sortFinished = time.time()
        time.sleep(self.task.processingTime)
        
        self.control.end_reduce_task(self.task)

        print "ReduceTask: finished (%s)" % self.task.name


class ReduceCopier(Thread):
    """
    Copy a partition from the map output data of a given map task.
    """

    def __init__(self, partition, copierControl):
        Thread.__init__(self)
        self.partition = partition
        self.copierControl = copierControl

    def run(self):
        self.copierControl.start_copy()

        self.partition.startTime = time.time()
        print "ReduceCopier: copiando partitcao - %s" % str(self.partition)

        self.copierControl.control.cond.acquire()
        global IPERF_PORT
        self.port = IPERF_PORT
        IPERF_PORT += 1
        self.copierControl.control.cond.release()
        self.partition.dstPort = self.port
                
        print "ReduceCopier: starting iperf server at %s:%d" % (self.partition.dstAddress, self.partition.dstPort)
        cmd = "./hadoop/iperf-server %s %d" % (self.partition.dstAddress, self.partition.dstPort)
        print "ReduceCopier: run %s" % cmd
        p = Popen(cmd, shell=True)
        
        ok = False
        while not ok:
            ok = self.contactShuffleServer()
            if not ok:
                time.sleep(0.1)

        # Wait for the shuffle transfer to finish
        p.wait()
        
        self.partition.finishTime = time.time()
        self.partition.duration = self.partition.finishTime - self.partition.startTime
        self.copierControl.control.end_shuffle(self.partition)
        self.copierControl.end_copy()

        print "ReduceCopier: finished %s:%d" % (self.partition.dstAddress, self.partition.dstPort)


    def contactShuffleServer(self):
        """Contact the shuffle server informing Do some things.
        :param verbose: Be verbose (give additional messages).
        """
        try:
            s = socket.socket()
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            host = self.partition.srcAddress
            port = SHUFFLESERVER_PORT
            s.connect((host, port))
            #print "ReduceCopier(" + name + ") got error connetion to " + host + ":" + str(port)
            data = pickle.dumps(self.partition, -1)
            s.sendall(data)
            #self.logger.info("ReduceCopier(" + self.partition.reducer + ") send to " + host + ": " + str(self.partition))
            data = s.recv(1024)
            s.close()
            return True
        except:
            print traceback.print_exc(None, sys.stderr)
            return False

class CopierControl():
    def __init__(self, control):
        self.finished_copier_threads = 0
        self.active_copier_threads = 0
        #self.logger = logger
        self.cond = Condition(Lock())
        self.control = control

    def start_copy(self):
        #print "Starting Thread-" + task.name
        self.cond.acquire()
        #self.logger.task_start(task.name)
        self.active_copier_threads += 1
        self.cond.release()

    def end_copy(self):
        #print "Finishing Thread-" + task.name
        self.cond.acquire()
        #self.logger.task_finish(task.name)
        self.active_copier_threads -= 1
        self.finished_copier_threads += 1
        self.cond.notify()
        self.cond.release()
