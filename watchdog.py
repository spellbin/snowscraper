import subprocess
import time
import os
import psutil
import logging
from pathlib import Path
import sys
import daemon  # pip install python-daemon

# Configuration
RESTART_DELAY = 5
SCRIPT_PATH = "./snowgui.py"  # Absolute path recommended
LOG_FILE = "./watchdog.log"  # System log location
HEARTBEAT_FILE = "./heartbeat.txt"
HEARTBEAT_TIMEOUT = 60  # seconds
MAX_MEMORY_MB = 250  # Maximum allowed memory in MB
CHECK_INTERVAL = 30  # seconds between checks

class WatchdogDaemon:
    def __init__(self):
        self.process = None
        self.heartbeat_file = Path(HEARTBEAT_FILE)
        self.setup_logging()
        
    def setup_logging(self):
        """Configure proper daemon logging"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(LOG_FILE),
                logging.StreamHandler(sys.stdout)
            ]
        )
        logging.info("Initializing Watchdog Daemon")

    def start_process(self):
        """Start the monitored process and log its output"""
        log_file = open("./snowgui.log", "a")  # File to store output
        self.process = subprocess.Popen(
            ["python3", SCRIPT_PATH],
            stdout=log_file,
            stderr=subprocess.STDOUT,  # Merge stderr into stdout
            text=True,  # Ensure text mode (Python 3.7+)
            bufsize=1,  # Line-buffered
        )
        logging.info(f"Started process with PID {self.process.pid}, logging to ./snowgui.log")
        return self.process

    def update_heartbeat(self):
        """Update the heartbeat timestamp"""
        try:
            with open(self.heartbeat_file, 'w') as f:
                f.write(str(time.time()))
        except Exception as e:
            logging.error(f"Heartbeat update failed: {e}")

    def check_heartbeat(self):
        """Check if process is responding via heartbeat"""
        try:
            if not self.heartbeat_file.exists():
                return False
            
            last_modified = self.heartbeat_file.stat().st_mtime
            return (time.time() - last_modified) <= HEARTBEAT_TIMEOUT
        except Exception as e:
            logging.error(f"Heartbeat check failed: {e}")
            return False

    def check_memory_usage(self):
        """Check if process is using too much memory"""
        if not self.process or self.process.poll() is not None:
            return False
        
        try:
            process = psutil.Process(self.process.pid)
            mem_info = process.memory_full_info()
            memory_mb = mem_info.rss / (1024 * 1024)  # RSS in MB
            
            if memory_mb > MAX_MEMORY_MB:
                logging.warning(f"Memory usage {memory_mb:.2f}MB exceeds limit {MAX_MEMORY_MB}MB")
                return True
            return False
        except psutil.NoSuchProcess:
            return False
        except Exception as e:
            logging.error(f"Memory check failed: {e}")
            return False

    def kill_process_tree(self):
        """Kill the process and all its child processes"""
        if not self.process:
            return
            
        try:
            parent = psutil.Process(self.process.pid)
            children = parent.children(recursive=True)
            
            # Kill children first
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            
            # Allow graceful termination
            gone, alive = psutil.wait_procs(children, timeout=5)
            for p in alive:
                p.kill()
            
            # Then kill parent
            parent.terminate()
            try:
                parent.wait(timeout=5)
            except psutil.TimeoutExpired:
                parent.kill()
            
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            logging.error(f"Error killing process tree: {e}")
            try:
                self.process.kill()
            except:
                pass
        finally:
            try:
                if self.heartbeat_file.exists():
                    self.heartbeat_file.unlink()
            except:
                pass

    def restart_process(self, reason="unknown"):
        """Restart the monitored process with a reason"""
        logging.info(f"Restarting process due to: {reason}")
        
        # First kill the old process
        self.kill_process_tree()
        
        # Wait before restarting
        time.sleep(RESTART_DELAY)
        
        # Start new process
        if not self.start_process():
            logging.error("Failed to restart process. Retrying...")
            time.sleep(RESTART_DELAY * 2)
            self.start_process()

    def is_process_running(self):
        """Check if the process is still running"""
        if self.process is None:
            return False
        return self.process.poll() is None

    def run(self):
        """Main monitoring loop"""
        logging.info("Starting watchdog daemon")
        logging.info(f"Config - Memory: {MAX_MEMORY_MB}MB, Heartbeat: {HEARTBEAT_TIMEOUT}s")
        
        if not self.start_process():
            logging.error("Initial process start failed. Exiting.")
            return
        
        while True:
            try:
                if not self.is_process_running():
                    self.restart_process("process crashed")
                
                # Check for hangs using heartbeat
                elif not self.check_heartbeat():
                    self.restart_process("heartbeat timeout (process hung)")
                
                # Check for memory leaks
                elif self.check_memory_usage():
                    self.restart_process("excessive memory usage")
                
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                logging.info("Watchdog received shutdown signal")
                self.kill_process_tree()
                break
            except Exception as e:
                logging.error(f"Watchdog error: {e}")
                time.sleep(CHECK_INTERVAL)  # Prevent tight error loops

def daemon_main():
    """Run as a proper daemon"""
    with daemon.DaemonContext(
        working_directory=os.path.dirname(os.path.abspath(__file__)),
        umask=0o002,
        prevent_core=False,
        files_preserve=[sys.stdout, sys.stderr]
    ):
        watchdog = WatchdogDaemon()
        watchdog.run()

if __name__ == "__main__":
    # Run as daemon if not in foreground mode
    if '--foreground' in sys.argv:
        WatchdogDaemon().run()
    else:
        daemon_main()
