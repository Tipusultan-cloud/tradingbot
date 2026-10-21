locals {
  common_tags = merge(var.tags, {
    environment = var.environment
    managed_by  = "terraform"
    project     = "tradingbot"
  })

  name_prefix = "tradingbot-${var.environment}"
}

module "resource_group" {
  source = "./modules/resource_group"

  name     = var.resource_group_name
  location = var.location
  tags     = local.common_tags
}

module "network" {
  source = "./modules/network"

  name                = "${local.name_prefix}-network"
  location            = module.resource_group.location
  resource_group_name = module.resource_group.name
  address_space       = var.vnet_address_space
  subnet_prefix       = var.subnet_address_prefix
  admin_source_cidr   = var.admin_source_cidr
  tags                = local.common_tags
}

module "tradingbot_vm" {
  source = "./modules/windows_vm"

  name                = "${local.name_prefix}-vm"
  location            = module.resource_group.location
  resource_group_name = module.resource_group.name
  size                = var.vm_size
  admin_username      = var.vm_admin_username
  admin_password      = var.vm_admin_password
  subnet_id           = module.network.subnet_id
  public_ip_id        = module.network.public_ip_id
  data_disk_size_gb   = var.data_disk_size_gb
  tags                = local.common_tags
}
