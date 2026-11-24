#!/usr/bin/env python3
"""
Oracle Cloud Always Free ARM VPS Auto-Provisioner
=================================================
Keeps asking Oracle Cloud for an Always Free Ampere A1 server (VM.Standard.A1.Flex) in the tenancy HOME REGION.
Oracle currently restricts Always Free A1 compute to the home region; this script deliberately refuses other regions.

Full guide (setup, every option, troubleshooting): https://www.codewing.app/oracle

What it does to stay safe:
1. Free limit: never requests more than 2 OCPUs and 12 GB RAM, the current Always Free A1 allocation for an
   Always Free tenancy. It checks existing A1 instances first.
2. Polite retries: every 2 minutes by default (+/- 10 seconds), never faster than once a minute.
3. No duplicates: before every try it checks whether a server with the same name already exists.
4. No double server on a network drop: each try sends a one-time retry token.
5. Clear errors: "out of capacity" (normal, it keeps trying) is told apart from a wrong key, a full quota or a
   rate limit (it stops or waits and tells you why).
6. Finds your public subnet by itself (or uses --subnet-id), and checks it has a route to the internet.
7. SSH key: uses your own public key; if you have none it makes one and saves the private key with 0600 permissions.

Nothing in this file is tied to anyone's account. It reads YOUR Oracle API key from YOUR config file
(~/.oci/config by default). Never share that file or the .pem key it points to.

Quick start:
    pip install oci
    python3 oci_free_vps.py --confirm-always-free

Free to use and change. No warranty: you run it on your own Oracle account.
"""

import os
import sys
import time
import uuid
import random
import argparse
import datetime
from pathlib import Path

try:
    import oci
except ImportError as e:
    print(f"Error: OCI Python SDK is not installed ({e}). Run: pip install oci")
    sys.exit(1)


# --- Defaults (change them with the command-line options, see --help) ---
DEFAULT_SHAPE = "VM.Standard.A1.Flex"
DEFAULT_OCPUS = 2.0                      # Always Free A1 allocation target
DEFAULT_MEMORY_IN_GBS = 12.0             # Always Free A1 allocation target
DEFAULT_BOOT_VOLUME_GB = 50              # Minimum boot volume; counts toward 200 GB home-region allowance
DEFAULT_DISPLAY_NAME = "oracle-arm-1"    # the server's name in the console
DEFAULT_CHECK_INTERVAL_SECONDS = 120     # 2 minutes between tries
DEFAULT_JITTER_SECONDS = 10              # +/- 10 seconds to avoid synchronized polling
MIN_INTERVAL_SECONDS = 60                # never ask faster than once a minute
DEFAULT_LOG_FILE = "oci_vps_hunter.log"

FREE_ARM_OCPUS = 2.0
FREE_ARM_MEMORY_GB = 12.0
MIN_BOOT_VOLUME_GB = 50
MAX_FREE_BLOCK_VOLUME_GB = 200


def log(message: str, log_file: str = DEFAULT_LOG_FILE):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    print(formatted, flush=True)
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass


def resolve_config_path(custom_path: str = None) -> str:
    """Finds the OCI config file: --config -> ~/.oci/config -> ./oci_config.ini -> ~/oci_hunter/oci_config.ini."""
    candidates = []
    if custom_path:
        candidates.append(Path(custom_path).expanduser())
    candidates.append(Path.home() / ".oci" / "config")
    candidates.append(Path("oci_config.ini"))
    candidates.append(Path.home() / "oci_hunter" / "oci_config.ini")

    for candidate in candidates:
        if candidate.resolve().exists():
            return str(candidate.resolve())

    raise FileNotFoundError(
        "Could not find your OCI config file. Create an API key in the Oracle Cloud console (Profile > My profile > "
        "API keys > Add API key) and save the config it shows to ~/.oci/config. Checked: " +
        ", ".join(str(c) for c in candidates)
    )


def load_oci_config(config_file: str) -> dict:
    config = oci.config.from_file(config_file, "DEFAULT")
    oci.config.validate_config(config)
    return config


def get_all_compartment_ids(identity_client, tenancy_id: str):
    """Return the tenancy root and all active child compartments (best-effort recursive listing)."""
    found = [tenancy_id]
    pending = [tenancy_id]
    while pending:
        parent_id = pending.pop()
        children = oci.pagination.list_call_get_all_results(
            identity_client.list_compartments, compartment_id=parent_id, lifecycle_state="ACTIVE"
        ).data
        for child in children:
            if child.id not in found:
                found.append(child.id)
                pending.append(child.id)
    return found


def check_existing_instance(compute_client, compartment_id: str, display_name: str, identity_client=None):
    """
    Duplicate protection: returns a running or starting server with this display_name, if there is one.
    """
    compartment_ids = get_all_compartment_ids(identity_client, compartment_id) if identity_client else [compartment_id]
    for current_compartment_id in compartment_ids:
        instances = oci.pagination.list_call_get_all_results(
            compute_client.list_instances, compartment_id=current_compartment_id
        ).data
        for inst in instances:
            if inst.lifecycle_state in ["TERMINATED", "TERMINATING"]:
                continue
            if inst.display_name == display_name:
                return inst
    return None


def check_arm_resource_limits(compute_client, compartment_id: str, requested_ocpus: float, requested_ram: float, identity_client=None):
    """
    Makes sure your ARM (A1.Flex) servers together stay within 2 OCPUs and 12 GB RAM after this one.
    """
    compartment_ids = get_all_compartment_ids(identity_client, compartment_id) if identity_client else [compartment_id]
    used_ocpus = 0.0
    used_ram = 0.0
    for current_compartment_id in compartment_ids:
        instances = oci.pagination.list_call_get_all_results(
            compute_client.list_instances, compartment_id=current_compartment_id
        ).data
        for inst in instances:
            if inst.lifecycle_state not in ["TERMINATED", "TERMINATING"] and "A1.Flex" in inst.shape:
                if inst.shape_config:
                    used_ocpus += (inst.shape_config.ocpus or 0.0)
                    used_ram += (inst.shape_config.memory_in_gbs or 0.0)

    log(f"Current ARM A1 Usage: {used_ocpus:.1f}/{FREE_ARM_OCPUS:.1f} OCPUs, {used_ram:.1f}/{FREE_ARM_MEMORY_GB:.1f} GB RAM")
    if (used_ocpus + requested_ocpus) > FREE_ARM_OCPUS or (used_ram + requested_ram) > FREE_ARM_MEMORY_GB:
        raise RuntimeError(
            f"Requested {requested_ocpus} OCPUs and {requested_ram} GB RAM would exceed "
            f"Always Free limits (Current: {used_ocpus} OCPUs, {used_ram} GB RAM, Max: 2 OCPUs, 12 GB RAM)."
        )


def check_block_volume_allowance(blockstorage_client, identity_client, tenancy_id: str, availability_domains: list[str], requested_boot_gb: int):
    """Check combined boot + block volume usage against the 200 GB Always Free home-region allowance."""
    compartment_ids = get_all_compartment_ids(identity_client, tenancy_id)
    used_gb = 0.0

    for current_compartment_id in compartment_ids:
        block_volumes = oci.pagination.list_call_get_all_results(
            blockstorage_client.list_volumes, compartment_id=current_compartment_id
        ).data
        for volume in block_volumes:
            if volume.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
                used_gb += float(volume.size_in_gbs or 0)

        for ad_name in availability_domains:
            boot_volumes = oci.pagination.list_call_get_all_results(
                blockstorage_client.list_boot_volumes,
                compartment_id=current_compartment_id,
                availability_domain=ad_name
            ).data
            for volume in boot_volumes:
                if volume.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
                    used_gb += float(volume.size_in_gbs or 0)

    log(f"Current home-region block storage: {used_gb:.1f}/{MAX_FREE_BLOCK_VOLUME_GB} GB")
    if used_gb + requested_boot_gb > MAX_FREE_BLOCK_VOLUME_GB:
        raise RuntimeError(
            f"Adding the requested {requested_boot_gb} GB boot volume would exceed the 200 GB Always Free "
            f"block-storage allowance (currently using {used_gb:.1f} GB)."
        )


def get_or_create_ssh_key(custom_key: str = None) -> str:
    """
    The SSH public key the new server will accept: --ssh-key (the key itself or a .pub file), else your own key
    in ~/.ssh, else a new keypair made here (oci_instance_key.pem / .pub, the private key with 0600 permissions).
    """
    if custom_key and custom_key.strip().startswith(("ssh-ed25519", "ssh-rsa", "ecdsa-")):
        log("Using the SSH public key given with --ssh-key")
        return custom_key.strip()

    candidates = []
    if custom_key:
        candidates.append(Path(custom_key).expanduser())

    home_ssh = Path.home() / ".ssh"
    candidates.extend([
        home_ssh / "id_ed25519.pub",
        home_ssh / "id_rsa.pub",
        home_ssh / "authorized_keys",
        Path("oci_instance_key.pub")
    ])

    for key_file in candidates:
        if key_file.exists():
            try:
                with open(key_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ssh-ed25519") or line.startswith("ssh-rsa") or line.startswith("ecdsa-"):
                            log(f"Using SSH public key from: {key_file}")
                            return line
            except Exception:
                continue

    log("No SSH public key found. Generating dedicated RSA keypair ('oci_instance_key')...")
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )

    priv_path = Path("oci_instance_key.pem")
    pub_path = Path("oci_instance_key.pub")

    with open(priv_path, "wb") as f:
        f.write(private_pem)

    try:
        os.chmod(priv_path, 0o600)
    except Exception:
        pass

    public_ssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH
    ).decode("utf-8").strip()

    with open(pub_path, "w", encoding="utf-8") as f:
        f.write(public_ssh + "\n")

    log(f"Generated SSH keypair: {priv_path.resolve()} (keep it private, you log in with it) and {pub_path.resolve()}")
    return public_ssh


def find_availability_domains(identity_client, compartment_id: str) -> list[str]:
    """Return every availability domain in the home region so capacity can be tried in each one."""
    ads = identity_client.list_availability_domains(compartment_id).data
    if not ads:
        raise RuntimeError("No Availability Domains found in the tenancy home region.")
    return [ad.name for ad in ads]


def find_public_subnet(core_client, compartment_id: str) -> str:
    """Finds a subnet in your account that gives servers a public IP (the one the VCN wizard makes)."""
    vcns = oci.pagination.list_call_get_all_results(core_client.list_vcns, compartment_id=compartment_id).data
    for vcn in vcns:
        if vcn.lifecycle_state != "AVAILABLE":
            continue
        subnets = oci.pagination.list_call_get_all_results(
            core_client.list_subnets, compartment_id=compartment_id, vcn_id=vcn.id
        ).data
        for subnet in subnets:
            if subnet.lifecycle_state == "AVAILABLE" and not subnet.prohibit_public_ip_on_vnic:
                log(f"Found public subnet '{subnet.display_name}' in VCN '{vcn.display_name}'")
                return subnet.id
    raise RuntimeError(
        "No public subnet found in your account. In the Oracle Cloud console open Networking > Virtual cloud networks, "
        "choose Actions > Start VCN Wizard > 'Create VCN with Internet Connectivity', create it, then run this "
        "script again (or pass --subnet-id with your subnet's OCID)."
    )


def verify_public_subnet(core_client, compartment_id: str, subnet_id: str) -> str:
    """Checks that the subnet exists, allows public IPs and has a route to an internet gateway."""
    try:
        subnet = core_client.get_subnet(subnet_id).data
    except Exception as e:
        log(f"Subnet verification failed for {subnet_id}: {e}")
        raise

    if subnet.prohibit_public_ip_on_vnic:
        log(f"Warning: Subnet '{subnet.display_name}' is private (no public IPs). You will not be able to SSH in "
            f"from the internet. Use a public subnet, or leave out --subnet-id to find one.")
    else:
        log(f"Verified Public Subnet: '{subnet.display_name}'")

    try:
        route_table = core_client.get_route_table(subnet.route_table_id).data
        has_internet = any(
            (rule.network_entity_id or "").startswith("ocid1.internetgateway")
            and (rule.destination or rule.cidr_block) == "0.0.0.0/0"
            for rule in (route_table.route_rules or [])
        )
        if not has_internet:
            log("Warning: this subnet's route table has no 0.0.0.0/0 route to an internet gateway, so the server "
                "may not be reachable. The VCN wizard ('Create VCN with Internet Connectivity') sets this up.")
    except Exception as e:
        log(f"Note: could not check the subnet's route table ({e}).")

    return subnet.id


def find_latest_arm_ubuntu_image(compute_client, compartment_id: str, shape: str, custom_image_id: str = None) -> str:
    if custom_image_id:
        return custom_image_id

    images = compute_client.list_images(
        compartment_id=compartment_id,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC"
    ).data

    for preferred in ["Canonical-Ubuntu-24.04", "Canonical-Ubuntu-22.04", "Canonical-Ubuntu"]:
        for img in images:
            if preferred.lower() in img.display_name.lower() and "minimal" not in img.display_name.lower():
                log(f"Selected OS Image: {img.display_name}")
                return img.id

    for img in images:
        if "ubuntu" in img.display_name.lower():
            log(f"Selected OS Image: {img.display_name}")
            return img.id

    if images:
        return images[0].id

    raise RuntimeError(f"No compatible images found for shape {shape}")


def wait_with_jitter(base_interval: int, jitter: int):
    """Waits base_interval +/- jitter seconds (13-17 minutes by default), never less than a minute."""
    min_delay = max(MIN_INTERVAL_SECONDS, base_interval - jitter)
    max_delay = max(min_delay, base_interval + jitter)
    delay = random.uniform(min_delay, max_delay)

    wake_time = datetime.datetime.now() + datetime.timedelta(seconds=delay)
    print(f"--> Next availability check in {delay / 60:.1f} mins (at {wake_time.strftime('%H:%M:%S')})...\n", flush=True)
    time.sleep(delay)


def launch_instance_safe(
    compute_client,
    compartment_id: str,
    ad_name: str,
    subnet_id: str,
    image_id: str,
    ssh_public_key: str,
    shape: str,
    ocpus: float,
    memory_in_gbs: float,
    boot_volume_gb: int,
    display_name: str,
    retry_token: str
):
    source_details = oci.core.models.InstanceSourceViaImageDetails(
        image_id=image_id,
        boot_volume_size_in_gbs=boot_volume_gb
    )

    vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=subnet_id,
        assign_public_ip=True,
        display_name=f"{display_name}-vnic"
    )

    metadata = {"ssh_authorized_keys": ssh_public_key}

    launch_kwargs = {
        "compartment_id": compartment_id,
        "availability_domain": ad_name,
        "display_name": display_name,
        "image_id": image_id,
        "shape": shape,
        "create_vnic_details": vnic_details,
        "metadata": metadata,
        "source_details": source_details
    }

    if "Flex" in shape:
        launch_kwargs["shape_config"] = oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=memory_in_gbs
        )

    launch_details = oci.core.models.LaunchInstanceDetails(**launch_kwargs)

    return compute_client.launch_instance(
        launch_instance_details=launch_details,
        opc_retry_token=retry_token
    )


def print_success_summary(instance, compute_client, network_client, compartment_id: str):
    log("=" * 65)
    log("SUCCESS! Oracle Cloud Always Free VPS Successfully Created!")
    log("=" * 65)
    log(f"Instance Name : {instance.display_name}")
    log(f"State         : {instance.lifecycle_state}")
    log(f"Shape         : {instance.shape}")
    log("=" * 65)

    log("Waiting for public IP assignment (takes ~30 seconds)...")
    time.sleep(20)

    try:
        vnics = compute_client.list_vnic_attachments(
            compartment_id=compartment_id,
            instance_id=instance.id
        ).data
        if vnics:
            vnic = network_client.get_vnic(vnics[0].vnic_id).data
            if vnic.public_ip:
                log(f"[+] Assigned Public IP : {vnic.public_ip}")
                log(f"[+] SSH Command        : ssh -i <your_private_key> ubuntu@{vnic.public_ip}")
                return
        log("Note: no public IP yet. Open Compute > Instances in the Oracle Cloud console to see it.")
    except Exception as e:
        log(f"Note: Could not query IP automatically ({e}). View IP in Oracle Cloud Console.")


def main():
    parser = argparse.ArgumentParser(description="Free-only Oracle Cloud A1 VPS hunter (2 OCPU / 12 GB, home region only)")
    parser.add_argument("--config", default=None, help="Path to your OCI config file (default: ~/.oci/config)")
    parser.add_argument("--shape", default=DEFAULT_SHAPE, help="Shape (default: VM.Standard.A1.Flex)")
    parser.add_argument("--ocpus", type=float, default=DEFAULT_OCPUS, help="OCPUs (default: 2; Always Free A1 tenancy limit: 2 total)")
    parser.add_argument("--memory", type=float, default=DEFAULT_MEMORY_IN_GBS, help="RAM in GB (default: 12; Always Free A1 tenancy limit: 12 total)")
    parser.add_argument("--boot-size", type=int, default=DEFAULT_BOOT_VOLUME_GB, help="Boot disk in GB (default: 50, minimum 50)")
    parser.add_argument("--name", default=DEFAULT_DISPLAY_NAME, help="Server name (default: oracle-arm-1)")
    parser.add_argument("--interval", type=int, default=DEFAULT_CHECK_INTERVAL_SECONDS, help="Seconds between tries (default: 120 = 2 min; minimum 60)")
    parser.add_argument("--jitter", type=int, default=DEFAULT_JITTER_SECONDS, help="Random +/- seconds added to each wait (default: 10)")
    parser.add_argument("--ssh-key", default=None, help="Your SSH public key: the key itself, or the path to a .pub file")
    parser.add_argument("--subnet-id", default=None, help="Your public subnet's OCID (default: found automatically)")
    parser.add_argument("--image-id", default=None, help="Image OCID (default: the latest Ubuntu for ARM)")
    parser.add_argument("--max-attempts", type=int, default=0, help="Max attempts (0 = run until created)")
    parser.add_argument("--confirm-always-free", action="store_true", help="Confirm you checked the Always Free eligibility label and storage allowance in the OCI Console")

    args = parser.parse_args()

    # Hard guardrails: this hunter must only request the Always Free A1 shape/configuration.
    if args.shape != DEFAULT_SHAPE:
        parser.error("Free-only mode supports only VM.Standard.A1.Flex; other shapes may be billable.")
    if args.ocpus <= 0 or args.memory <= 0 or args.ocpus > FREE_ARM_OCPUS or args.memory > FREE_ARM_MEMORY_GB:
        parser.error("Free-only limit is at most 2 OCPUs and 12 GB RAM in total.")
    if args.boot_size < MIN_BOOT_VOLUME_GB or args.boot_size > MAX_FREE_BLOCK_VOLUME_GB:
        parser.error("Boot volume must be between 50 and 200 GB; check your existing home-region block-volume usage too.")
    if not args.confirm_always_free:
        parser.error("Before launching, verify the A1 shape is marked Always Free-eligible and that total home-region block-volume usage will stay within 200 GB. Then rerun with --confirm-always-free.")

    log("=" * 65)
    log("Oracle Cloud Always Free A1 VPS Hunter (2 OCPU / 12 GB)")
    log("=" * 65)

    if args.interval < MIN_INTERVAL_SECONDS:
        log(f"--interval {args.interval} is too fast: using {MIN_INTERVAL_SECONDS} seconds (Oracle limits how often you can ask).")
        args.interval = MIN_INTERVAL_SECONDS

    try:
        config_file = resolve_config_path(args.config)
        log(f"Using OCI Config: {config_file}")
        config = load_oci_config(config_file)
        compartment_id = config["tenancy"]
        log(f"Configured region: {config.get('region')}")
    except Exception as e:
        log(f"Fatal Configuration Error: {e}")
        sys.exit(1)

    identity_client = oci.identity.IdentityClient(config)
    try:
        tenancy = identity_client.get_tenancy(compartment_id).data
        regions = identity_client.list_regions().data
        home_region = next((r.name for r in regions if r.key == tenancy.home_region_key), None)
        if not home_region:
            raise RuntimeError("Could not resolve the tenancy home region from Oracle's region list.")
        if config.get("region") != home_region:
            raise RuntimeError(
                f"This Always Free A1 hunter must run in your tenancy HOME region ({home_region}), "
                f"but your OCI config is set to {config.get('region')}. Update the region in ~/.oci/config. "
                "Oracle's Always Free A1 compute allocation is restricted to the home region."
            )
        log(f"Verified tenancy home region: {home_region}")
    except Exception as e:
        log(f"Home-region verification failed: {e}")
        sys.exit(1)

    core_client = oci.core.VirtualNetworkClient(config)
    compute_client = oci.core.ComputeClient(config)
    blockstorage_client = oci.core.BlockstorageClient(config)

    # 1. Check if instance with this name already exists
    try:
        existing = check_existing_instance(compute_client, compartment_id, args.name, identity_client)
    except oci.exceptions.ServiceError as e:
        log(f"[FATAL] Oracle refused the first request ({e.status} - {e.code}): {e.message}")
        log("Check the user, fingerprint, tenancy, region and key_file lines in your config file.")
        sys.exit(1)
    if existing:
        log(f"[DUPLICATE SHIELD] Found existing instance '{existing.display_name}' in state '{existing.lifecycle_state}'.")
        log(f"Aborting hunt: You already have '{args.name}' provisioned.")
        print_success_summary(existing, compute_client, core_client, compartment_id)
        sys.exit(0)

    # 2. Check total ARM resource limits to prevent exceeding Always Free tier
    if "A1.Flex" in args.shape:
        try:
            check_arm_resource_limits(compute_client, compartment_id, args.ocpus, args.memory, identity_client)
        except Exception as e:
            log(f"[RESOURCE LIMIT ERROR] {e}")
            sys.exit(1)

    # 3. Resolve Resources
    try:
        ad_names = find_availability_domains(identity_client, compartment_id)
        log("Availability Domains: " + ", ".join(ad_names))
        check_block_volume_allowance(blockstorage_client, identity_client, compartment_id, ad_names, args.boot_size)

        subnet_id = args.subnet_id or find_public_subnet(core_client, compartment_id)
        subnet_id = verify_public_subnet(core_client, compartment_id, subnet_id)
        image_id = find_latest_arm_ubuntu_image(compute_client, compartment_id, args.shape, args.image_id)
        ssh_public_key = get_or_create_ssh_key(args.ssh_key)
    except Exception as e:
        log(f"Resource Initialization Error: {e}")
        sys.exit(1)

    log(f"Target Instance     : {args.name} | Shape: {args.shape} ({args.ocpus} OCPU, {args.memory} GB RAM)")
    log(f"Boot Volume Size    : {args.boot_size} GB")
    log(f"Retry Schedule      : {args.interval}s (+/- {args.jitter}s jitter, never under {MIN_INTERVAL_SECONDS}s)")
    log("Entering safe capacity monitoring loop...")
    log("-" * 65)

    attempts = 0

    while True:
        attempts += 1

        # Check for duplicate instance name before every capacity round.
        try:
            existing = check_existing_instance(compute_client, compartment_id, args.name, identity_client)
        except Exception as e:
            log(f"Could not list instances this time ({e}); trying again after the wait.")
            wait_with_jitter(args.interval, args.jitter)
            continue
        if existing:
            log(f"[DUPLICATE SHIELD] Instance '{existing.display_name}' was detected. Stopping loop.")
            print_success_summary(existing, compute_client, core_client, compartment_id)
            break

        created_instance = None
        should_back_off = False

        # Try every AD in the tenancy home region during this round.
        for ad_name in ad_names:
            retry_token = str(uuid.uuid4())
            log(f"[Attempt #{attempts}] Trying {args.shape} in availability domain {ad_name}...")
            try:
                response = launch_instance_safe(
                    compute_client=compute_client,
                    compartment_id=compartment_id,
                    ad_name=ad_name,
                    subnet_id=subnet_id,
                    image_id=image_id,
                    ssh_public_key=ssh_public_key,
                    shape=args.shape,
                    ocpus=args.ocpus,
                    memory_in_gbs=args.memory,
                    boot_volume_gb=args.boot_size,
                    display_name=args.name,
                    retry_token=retry_token
                )
                created_instance = response.data
                log(f"SUCCESS! Instance created with state: {created_instance.lifecycle_state}")
                break

            except oci.exceptions.ServiceError as e:
                is_capacity_exhaustion = (
                    e.code == "OutOfCapacity" or
                    (e.status == 500 and "out of host capacity" in str(e.message).lower())
                )

                if is_capacity_exhaustion:
                    log(f"Capacity unavailable in {ad_name}; checking the next availability domain.")
                    continue

                if e.status == 429 or "TooManyRequests" in str(e.code):
                    retry_after = 600
                    if hasattr(e, "headers") and e.headers and "Retry-After" in e.headers:
                        try:
                            retry_after = int(e.headers["Retry-After"])
                        except Exception:
                            pass
                    log(f"[HTTP 429] Rate limit detected. Backing off for {retry_after}s...")
                    time.sleep(retry_after)
                    should_back_off = True
                    break

                if e.status == 401 or "NotAuthenticated" in str(e.code):
                    log(f"[FATAL AUTH ERROR] Authentication failed ({e.status} - {e.code}): {e.message}")
                    log("Check the fingerprint and key_file in your config file, and that the API key is still in the console.")
                    sys.exit(1)

                if "LimitExceeded" in str(e.code) or "quota" in str(e.message).lower():
                    log(f"[FATAL QUOTA ERROR] Resource limit reached: {e.message}")
                    sys.exit(1)

                log(f"OCI Error in {ad_name} ({e.status} - {e.code}): {e.message}")
                # Non-capacity errors are unlikely to improve in another AD; wait before retrying.
                should_back_off = True
                break

            except Exception as e:
                log(f"Network / Client Exception in {ad_name}: {e}")
                log("Reconciling instance state with Oracle Cloud...")
                try:
                    existing = check_existing_instance(compute_client, compartment_id, args.name, identity_client)
                except Exception:
                    existing = None
                if existing:
                    created_instance = existing
                    break
                should_back_off = True
                break

        if created_instance:
            print_success_summary(created_instance, compute_client, core_client, compartment_id)
            break

        if args.max_attempts > 0 and attempts >= args.max_attempts:
            log(f"Reached max attempts ({args.max_attempts}). Exiting.")
            break

        if not should_back_off:
            log("No capacity found in any availability domain. Waiting before the next round.")
        wait_with_jitter(args.interval, args.jitter)


if __name__ == "__main__":
    main()
