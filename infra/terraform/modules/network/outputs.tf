output "subnet_id" {
  description = "Subnet ID for the Windows VM network interface."
  value       = azurerm_subnet.this.id
}

output "public_ip_id" {
  description = "Public IP resource ID attached to the Windows VM."
  value       = azurerm_public_ip.this.id
}

output "public_ip_address" {
  description = "Public IP address used for trusted-source RDP access."
  value       = azurerm_public_ip.this.ip_address
}
