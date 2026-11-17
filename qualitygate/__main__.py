import argparse
import logging
from pathlib import Path
import sys

from .config import Config
from .demo import seed
from .server import make_server
from .service import Service
from .worker import Worker


def main():
    parser=argparse.ArgumentParser(description="QualityGate 本地数据质量巡检平台")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--data-dir",type=Path,default=Path(".qualitygate"))
    parser.add_argument("--demo",action="store_true",help="加载合成演示数据并首次运行巡检")
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
    service=Service(Config(data_dir=args.data_dir.resolve(),port=args.port))
    # OS lock prevents a second application process from recovering another worker's jobs.
    lock=open(service.config.data_dir/"process.lock","a+b")
    lock.seek(0); lock.write(b"0"); lock.flush(); lock.seek(0)
    try:
        if sys.platform=="win32":
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("该数据目录已有 QualityGate 进程运行；请关闭旧进程或指定不同 --data-dir")
    server=make_server(service)
    if args.demo:
        demo=seed(service)
        if not service.jobs()["total"]:
            service.enqueue({"dataset_id":demo["dirty_dataset_id"],"ruleset_id":demo["ruleset_id"]},key="initial-demo-v1")
    worker=Worker(service); server.worker=worker; worker.start()
    print(f"\nQualityGate is running: http://127.0.0.1:{server.server_address[1]}\nPress Ctrl+C to stop.\n",flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close(); worker.stop(); lock.close()


if __name__=="__main__": main()
