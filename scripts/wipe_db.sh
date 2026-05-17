#!/bin/bash
sudo systemctl stop fastapi worker listener
rm -f ~/repos/Inventory/inventory.db
sudo systemctl start fastapi
sleep 3
sudo systemctl start worker listener
echo "Done. Database wiped and services restarted."
