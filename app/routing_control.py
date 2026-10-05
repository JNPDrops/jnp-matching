"""Routing assignment only; does not execute or retry any financial operation.

Run inside the existing Render environment; do not copy DATABASE_URL or tokens.
"""
import argparse
import json
import os

from operations import worker_coordination as c
from operations.routing_role import LEGACY, WORKER, request_handover


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'to-worker', 'to-web', 'pause'))
    args = parser.parse_args(argv)
    database_url = os.getenv('DATABASE_URL', '')
    division = int(os.getenv('EXACT_DIVISION', '3977752'))
    if not database_url:
        parser.exit(2, 'database_required\n')
    import psycopg
    try:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
            if args.action == 'status':
                conn.execute('SET default_transaction_read_only = on')
            else:
                c.initialize(conn)
                target = {'to-worker': WORKER, 'to-web': LEGACY, 'pause': None}[args.action]
                request_handover(conn, division, target)
            result = c.status_snapshot(conn, division)
            result['roles'] = [row for row in result['roles'] if row['role'] == 'routing']
            print(json.dumps(result, sort_keys=True))
    except Exception as error:
        # No DSNs, SQL parameters, secrets or customer records in CLI failures.
        parser.exit(2, 'routing_control_failed:' + type(error).__name__ + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
