# NVIS Observability Appliance (nvis-ova)
Turnkey observability for NVIDIA DGX / SuperPOD clusters — metrics, logs, fabric health,
per-Slurm-job benchmark tracking. Install with one interactive command:

    ./installer/install.sh        # interviews you for BMC/UFM/NetQ/Slurm-logs/NVLMapper, then deploys

See docs/ARCHITECTURE.md. This tree is generated; site secrets are NEVER committed (see .gitignore).
