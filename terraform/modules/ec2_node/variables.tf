variable "name" { type = string }
variable "node_label" {
  description = "Nombre visible de la máquina (WH-001, CENTRAL...)"
  type        = string
}
variable "component" {
  description = "Qué levanta manage_warehouse.sh en esta máquina: wh_001, wh_002... o central"
  type        = string
}
variable "ami_id" { type = string }
variable "instance_type" { type = string }
variable "subnet_id" { type = string }
variable "security_group_ids" { type = list(string) }
variable "key_name" { type = string }
variable "instance_profile_name" { type = string }
variable "root_volume_size_gb" { type = number }
variable "repo_url" { type = string }
variable "repo_branch" { type = string }
variable "runtime_env" {
  description = "Variables propias de esta máquina (se escriben en docker/components/<componente>/runtime.env)"
  type        = map(string)
  default     = {}
}
variable "advertise_port" {
  description = "Puerto de la API que la central usará para llamar a esta máquina por su IP privada (0 = no aplica)"
  type        = number
  default     = 0
}
