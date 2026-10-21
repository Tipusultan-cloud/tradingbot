variable "name" {
  description = "Name of the TradingBot Windows VM."
  type        = string
}

variable "location" {
  description = "Azure region for the VM."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that owns the VM."
  type        = string
}

variable "size" {
  description = "Azure VM size."
  type        = string
}

variable "admin_username" {
  description = "Local Windows administrator account name."
  type        = string
}

variable "admin_password" {
  description = "Local Windows administrator password."
  type        = string
  sensitive   = true
  nullable    = false
}

variable "subnet_id" {
  description = "Subnet ID for the VM network interface."
  type        = string
}

variable "public_ip_id" {
  description = "Public IP resource ID for trusted-source RDP access."
  type        = string
}

variable "data_disk_size_gb" {
  description = "Size of the separate persistent managed data disk."
  type        = number
}

variable "tags" {
  description = "Tags applied to VM resources."
  type        = map(string)
  default     = {}
}

resource "azurerm_network_interface" "this" {
  name                = "${var.name}-nic"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = var.public_ip_id
  }
}

resource "azurerm_windows_virtual_machine" "this" {
  name                = var.name
  computer_name       = "tradingbotvm"
  location            = var.location
  resource_group_name = var.resource_group_name
  size                = var.size
  admin_username      = var.admin_username
  admin_password      = var.admin_password
  network_interface_ids = [
    azurerm_network_interface.this.id,
  ]

  provision_vm_agent        = true
  automatic_updates_enabled = true
  patch_mode                = "AutomaticByPlatform"
  tags                      = var.tags

  source_image_reference {
    publisher = "MicrosoftWindowsServer"
    offer     = "WindowsServer"
    sku       = "2022-datacenter-azure-edition"
    version   = "latest"
  }

  os_disk {
    name                 = "${var.name}-osdisk"
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = 128
  }

  boot_diagnostics {}
}

resource "azurerm_managed_disk" "data" {
  name                 = "${var.name}-data"
  location             = var.location
  resource_group_name  = var.resource_group_name
  storage_account_type = "StandardSSD_LRS"
  create_option        = "Empty"
  disk_size_gb         = var.data_disk_size_gb
  tags                 = var.tags
}

resource "azurerm_virtual_machine_data_disk_attachment" "data" {
  managed_disk_id    = azurerm_managed_disk.data.id
  virtual_machine_id = azurerm_windows_virtual_machine.this.id
  lun                = 0
  caching            = "ReadWrite"
}
