output "name" {
  description = "Name of the TradingBot Windows VM."
  value       = azurerm_windows_virtual_machine.this.name
}

output "data_disk_id" {
  description = "ID of the persistent managed data disk attached to the VM."
  value       = azurerm_managed_disk.data.id
}
