variable "environment" {
  description = "Environment name used in Azure resource tags."
  type        = string
  default     = "dev"
}

variable "location" {
  description = "Azure region for the resource group."
  type        = string
  default     = "eastus2"
}

variable "resource_group_name" {
  description = "Name of the Azure resource group."
  type        = string
  default     = "tradingbot-dev-rg"
}

variable "vm_size" {
  description = "Azure VM size. Check regional availability and pricing before deployment."
  type        = string
  default     = "Standard_B2ms"
}

variable "vm_admin_username" {
  description = "Local Windows administrator account name for the VM."
  type        = string
  default     = "tradingbotadmin"
}

variable "vm_admin_password" {
  description = "Local Windows administrator password. Supply using TF_VAR_vm_admin_password, not a tfvars file."
  type        = string
  sensitive   = true
  nullable    = false

  validation {
    condition     = length(var.vm_admin_password) >= 16
    error_message = "Use a unique Windows VM administrator password with at least 16 characters."
  }
}

variable "admin_source_cidr" {
  description = "Your trusted public IPv4 CIDR allowed to connect to RDP, for example 203.0.113.10/32."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9.]+/[0-9]{1,2}$", var.admin_source_cidr)) && can(cidrhost(var.admin_source_cidr, 0)) && tonumber(split("/", var.admin_source_cidr)[1]) > 0
    error_message = "admin_source_cidr must be a valid IPv4 CIDR such as 203.0.113.10/32."
  }
}

variable "vnet_address_space" {
  description = "Private IPv4 address range for the virtual network."
  type        = string
  default     = "10.42.0.0/16"
}

variable "subnet_address_prefix" {
  description = "Private IPv4 address range for the VM subnet."
  type        = string
  default     = "10.42.1.0/24"
}

variable "data_disk_size_gb" {
  description = "Size of the separate persistent managed disk attached to the VM."
  type        = number
  default     = 64

  validation {
    condition     = var.data_disk_size_gb >= 32
    error_message = "data_disk_size_gb must be at least 32 GB."
  }
}

variable "tags" {
  description = "Additional tags applied to the resource group."
  type        = map(string)
  default     = {}
}
