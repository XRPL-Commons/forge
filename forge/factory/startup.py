"""Image-owned startup boundary, acknowledged by the parent before execution."""
import hashlib
import json
import os
import runpy
import traceback


def failure(error):
    if type(error).__name__ not in {'ImportError', 'ModuleNotFoundError', 'NameError', 'UnboundLocalError', 'SyntaxError', 'IndentationError', 'TabError'}:
        return None
    frames = [frame for frame in traceback.extract_tb(error.__traceback__)
              if frame.filename.startswith('/opt/forge/')]
    if not frames:
        return None
    frame = frames[-1]
    filename, line = frame.filename, frame.lineno
    if (isinstance(error, SyntaxError) and isinstance(error.filename, str)
            and error.filename.startswith('/opt/forge/') and len(error.filename) <= 240
            and type(error.lineno) is int and error.lineno > 0):
        filename, line = error.filename, error.lineno
    value = {'version': 1, 'phase': 'before_agent', 'exception': type(error).__name__,
             'file': filename, 'line': line}
    value['signature'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def main():
    try:
        runpy.run_path('/opt/forge/harness.py', run_name='__main__')
    except Exception as error:
        # The harness removes the descriptor before invoking provider code. No
        # later exception or worker output can become a startup classification.
        descriptor = os.environ.pop('FORGE_STARTUP_FD', None)
        if descriptor is not None:
            value = failure(error)
            if value is not None:
                os.write(int(descriptor), (json.dumps(value) + '\n').encode())
            os.close(int(descriptor))
        raise


if __name__ == '__main__':
    main()
