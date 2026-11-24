"""Windows equivalent of the flock calls used by the archiver."""
import os
import time
import msvcrt


class fcntl:
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    @staticmethod
    def flock(file, operation):
        descriptor = file.fileno() if hasattr(file, 'fileno') else file
        if os.fstat(descriptor).st_size == 0: os.write(descriptor, b'\0')
        os.lseek(descriptor, 0, os.SEEK_SET)
        if operation & fcntl.LOCK_UN:
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1); return
        while True:
            try: msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1); return
            except OSError:
                if operation & fcntl.LOCK_NB: raise BlockingIOError('Archive is already running') from None
                time.sleep(0.1)
