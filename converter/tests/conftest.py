import os
import sys

os.environ.setdefault("OMP_NUM_THREADS", "2")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "bench"))
