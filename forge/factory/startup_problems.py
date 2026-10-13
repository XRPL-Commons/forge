"""Classify only image-entrypoint proof of a failure before agent execution."""
import json
import re
from .job import startup_receipt


def validated_receipt(receipt, job_id):
    if not isinstance(receipt, dict) or receipt.get('attempt') != job_id:
        return None
    return startup_receipt(json.dumps({k:v for k,v in receipt.items() if k != 'attempt'}).encode(), 1)


def problem(job, capture):
    execution = capture.get('execution') or {}
    image = execution.get('image_id')
    if (execution.get('startup_protocol') != '1' or execution.get('exit_code') != 78
            or execution.get('running') is not False or execution.get('oom_killed')
            or not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)
            or execution.get('id') != job.get('container_id') or not job.get('container_id')
            or capture.get('result') is not None or capture.get('timed_out')):
        return None
    receipt = validated_receipt(capture.get('forge_startup'), job['id'])
    if receipt is None:
        return None
    return {**receipt, 'owner': 'forge', 'reason': 'harness_startup',
            'image_id': image, 'job_id': job['id'], 'capture_job_id': job['id']}
