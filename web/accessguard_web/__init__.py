"""AccessGuard control plane: accounts, projects, scan jobs, findings, billing and assistants.

The control plane never holds target credentials and never contacts customer targets; a
runner in the customer's environment executes approved scans (docs/adr/0009).
"""
