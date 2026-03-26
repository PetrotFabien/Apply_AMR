#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

try:
    from app import app
    print("✅ Import successful - no routing errors")
except Exception as e:
    print(f"❌ Import failed: {e}")
    sys.exit(1)