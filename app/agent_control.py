"""Assign a supported background role; never execute financial work from this CLI."""
import argparse
import json
import os

from operations import worker_coordination as c
from operations.assigned_role import ROLES, owner_for, request_handover


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=sorted(ROLES), required=True)
    parser.add_argument('action', choices=('status', 'to-worker', 'to-web', 'pause'))
    args = parser.parse_args(argv)
    database_url = os.getenv('DATABASE_URL', '')
    if not database_url:
        parser.exit(2, 'database_required\n')
    import psycopg
    try:
        division = int(os.getenv('EXACT_DIVISION', '3977752'))
        with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
            if args.action == 'status':
                conn.execute('SET default_transaction_read_only = on')
            else:
                c.initialize(conn)
                location = {'to-worker':'worker', 'to-web':'web', 'pause':None}[args.action]
                target = owner_for(args.role, location) if location else None
                request_handover(conn, division, args.role, target)
            result = c.status_snapshot(conn, division)
            result['roles'] = [row for row in result['roles'] if row['role'] == args.role]
            print(json.dumps(result, sort_keys=True))
    except Exception as error:
        parser.exit(2, 'agent_control_failed:' + type(error).__name__ + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
