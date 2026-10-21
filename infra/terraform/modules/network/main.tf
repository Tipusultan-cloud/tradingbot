variable "name" {
  description = "Prefix for network resources."
  type        = string
}

variable "location" {
  description = "Azure region for network resources."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group that owns network resources."
  type        = string
}

variable "address_space" {
  description = "Private IPv4 address range for the virtual network."
  type        = string
}

variable "subnet_prefix" {
  description = "Private IPv4 address range for the VM subnet."
  type        = string
}

variable "admin_source_cidr" {
  description = "Trusted IPv4 CIDR allowed to connect to RDP."
  type        = string
}

variable "tags" {
  description = "Tags applied to network resources."
  type        = map(string)
  default     = {}
}

resource "azurerm_virtual_network" "this" {
  name                = "${var.name}-vnet"
  address_space       = [var.address_space]
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_subnet" "this" {
  name                 = "${var.name}-subnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.subnet_prefix]
}

resource "azurerm_network_security_group" "this" {
  name                = "${var.name}-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "AllowRdpFromTrustedAddress"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3389"
    source_address_prefix      = var.admin_source_cidr
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "this" {
  subnet_id                 = azurerm_subnet.this.id
  network_security_group_id = azurerm_network_security_group.this.id
}

resource "azurerm_public_ip" "this" {
  name                = "${var.name}-public-ip"
  location            = var.location
  resource_group_name = var.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}
