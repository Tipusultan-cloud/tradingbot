# Azure TradingBot infrastructure

This modular Terraform configuration builds an Azure foundation for the
existing Windows-first TradingBot:

- A resource group, virtual network, subnet, and network security group.
- A Windows Server 2022 VM with a static public IP for RDP, allowed only from
  a trusted IPv4 CIDR that you supply.
- A separate persistent managed data disk attached to the VM.
- No public inbound access to the webhook or dashboard. TradingView webhooks
  can continue to reach the app through an outbound ngrok tunnel.

It does **not** install or deploy the TradingBot application, initialize or
mount the data disk, configure ngrok, or change the existing Task Scheduler
task to run when no user is logged in. Those are application deployment
steps, not created by this infrastructure. No Azure resources have been
provisioned.

## Prerequisites

- Terraform CLI 1.5 or later
- Azure CLI
- An Azure subscription and permission to create resource groups
- A stable public IPv4 address/CIDR for restricting RDP access

Authenticate locally; do not put credentials in Terraform files:

```powershell
az login
az account show --query "{name:name, id:id}" --output table
$env:ARM_SUBSCRIPTION_ID = az account show --query id --output tsv
```

## Review the infrastructure plan

Run these commands from `infra/terraform`. Replace the example RDP address
with your own trusted public IP in CIDR notation (usually `a.b.c.d/32`).

```powershell
Copy-Item terraform.tfvars.example terraform.tfvars
terraform init
terraform fmt -recursive
terraform validate
$securePassword = Read-Host "Enter a unique 16+ character VM password" -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $env:TF_VAR_vm_admin_password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    terraform plan
}
finally {
    Remove-Item Env:TF_VAR_vm_admin_password -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    $securePassword.Dispose()
}
```

The password is supplied through an environment variable rather than saved
in `terraform.tfvars`. Use a unique strong password and do not paste it into
chat. The plan should include a Windows VM, network resources, a public IP,
and managed disks. Review the full plan and current Azure pricing before
running `terraform apply`; a running VM, public IP, and disks incur ongoing
charges. Stop/deallocate the VM when you do not need it. A deallocated VM
does not run scans.

The sample `admin_source_cidr` is a documentation-only TEST-NET address and
will not allow your computer to connect. Replace it with your current trusted
public IP. Never open RDP to `0.0.0.0/0`. If your public IP changes, update
the CIDR and apply the reviewed NSG change.

Only the configured CIDR can connect to RDP (TCP 3389). The NSG does not
allow inbound ports 8000 or 8001; keep the dashboard private and use an
outbound tunnel for TradingView webhooks. The VM has outbound internet access
for market data and ngrok.

The data disk is attached but is initially unformatted. Before using it,
initialize it in Windows Disk Management and decide which project data to
move. Terraform does not copy your local source code, `.env`, scan outputs,
or SQLite journal to Azure. Back up data before changing VM or disk resources.

Terraform stores local state under this directory after initialization. Keep
state, saved plan files, and real `.tfvars` files private; state can contain
the VM admin password even when Terraform marks it sensitive. The local
ignore rules exclude state and real `.tfvars`. Local state is suitable only
for an initial single-user deployment, not team or production use. Before
team use, configure secured remote state with access control, versioning, and
locking.

## Modules

- The root module composes the resources and applies common tags.
- `modules/resource_group` owns the Azure resource group.
- `modules/network` owns the VNet, subnet, RDP-restricted NSG, and public IP.
- `modules/windows_vm` owns the Windows VM, NIC, OS disk, and persistent data
  disk.

## Next deployment tasks

1. Deploy only after reviewing the plan and expected Azure charges.
2. Connect with RDP from the allowed address and initialize the data disk.
3. Choose a private source-code deployment method; do not copy `.env` into
   source control or Terraform configuration.
4. Deploy the app, verify API/dashboard bindings remain private, and configure
   ngrok and the paper-only journal on the VM.
5. Update the scheduled task to run unattended under a least-privilege
   service identity; the current installer uses an interactive logon.
6. Add monitoring, backups, secured remote Terraform state, and CI validation
   before treating the deployment as production-ready.
