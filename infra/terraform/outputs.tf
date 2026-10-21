output "resource_group_id" {
  description = "Azure resource ID of the managed resource group."
  value       = module.resource_group.id
}

output "resource_group_name" {
  description = "Name of the managed resource group."
  value       = module.resource_group.name
}

output "resource_group_location" {
  description = "Azure region of the managed resource group."
  value       = module.resource_group.location
}

output "vm_name" {
  description = "Name of the TradingBot Windows VM."
  value       = module.tradingbot_vm.name
}

output "vm_public_ip" {
  description = "Public IP for RDP access, restricted by the configured admin source CIDR."
  value       = module.network.public_ip_address
}

output "vm_data_disk_id" {
  description = "ID of the persistent data disk attached to the VM."
  value       = module.tradingbot_vm.data_disk_id
}
