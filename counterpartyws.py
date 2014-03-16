#! /usr/bin/env python3

import argparse
import binascii
import sys
import traceback
import logging
import os
import decimal
import time
import json
import logging
import requests as httpclient
from requests.auth import HTTPBasicAuth

import bottle
from bottle import route, run, template, Bottle, request, static_file, redirect, error, hook, response, abort, auth_basic

from counterpartyd.lib import (config, util, exceptions, bitcoin)
from counterpartyd.lib import (send, order, btcpay, issuance, broadcast, bet, dividend, burn, cancel, callback)

from helpers import set_options, init_logging, D, S, DecimalEncoder, connect_to_db, check_auth, wallet_unlock, write_pid

app = Bottle()
set_options()
init_logging()
db = connect_to_db(10000)


counterpartyd_params = {
    'send': ['source', 'destination', 'quantity', 'asset'],
    'order': ['source', 'give_quantity', 'give_asset', 'get_quantity', 'get_asset', 'expiration', 'fee_fraction_required', 'fee_fraction_provided'],
    'btcpay': ['order_match_id'],
    'cancel': ['offer_hash'],
    'issuance': ['source', 'transfer_destination', 'asset_name', 'quantity', 'divisible', 'callable', 'call_date', 'call_price', 'description'],
    'dividend': ['source', 'asset', 'quantity_per_share', 'dividend_asset'],
    'callback': ['source', 'asset', 'fraction_per_share'],
    'broadcast': ['source', 'text', 'value', 'fee_fraction'],
    'bet': ['source', 'feed_address', 'bet_type', 'deadline', 'wager', 'counterwager', 'target_value', 'leverage', 'expiration']
}

def getp(key, default=''):    
    value = request.forms.get(key)
    if value is None or value=='':
        return default
    return value

def generate_unsigned_hex(tx_info):
    try:
        if config.MODE=="gui":
            unsigned_tx_hex = bitcoin.transaction(tx_info, config.MULTISIG)
            return {'success':True, 'message':str(unsigned_tx_hex)}
        else:
            pubkey = getp('pubkey')
            if pubkey!='':
                unsigned_tx_hex = bitcoin.transaction(tx_info, pubkey)
                return {'success':True, 'message':str(unsigned_tx_hex)}
            else:
                return {'success':False, 'message':'Source pubkey required'}
    except Exception as e:
        return {'success':False, 'message':str(e)}

def composer_request(path, method='GET', data={}):
    composer_url = 'http://'+config.COMPOSER_HOST+':'+str(config.COMPOSER_PORT)+path
    composer_auth = HTTPBasicAuth('', '')
    if method=='POST':
        composer_response = httpclient.post(composer_url, data=data, auth=composer_auth)
    else:
        composer_response = httpclient.get(composer_url, auth=composer_auth)
    result = composer_response.json()
    return result

@app.hook('after_request')
def enable_cors():
    # set CORS headers
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token'

    if bottle.request.method == 'OPTIONS':
        return ""

@app.hook('before_request')
def auth_user():
    if config.MODE=='gui':
        check_auth(request)

@app.route('/')
def index():
    page = "counterpartygui.html"
    if config.MODE=="composer":
        page = "composer.html"
    return static_file(page, root=config.GUI_DIR)


@app.route('/addresses/<address>')
def get_address(address):
    try:
        address_info = util.get_address(db, address=address)
        result = {"success":True, "message":address_info}        
    except Exception as e:
        result = {"success":True, "message":str(e)}
    return json.dumps(result, cls=DecimalEncoder)   


@app.route('/btcpay/<order_match_id>/source')
def btcpay_source(order_match_id):
    try:
        tx_info = btcpay.compose(db, order_match_id)
        result = {'success':True, 'message':tx_info[0]}
    except Exception as e:
        result = {'success':False, 'message':str(e)}
    return json.dumps(result, cls=DecimalEncoder) 


@app.route('/cancel/<offer_hash>/source')
def cancel_source(offer_hash):
    try:
        tx_info = cancel.compose(db, offer_hash)
        result = {'success':True, 'message':tx_info[0]}
    except Exception as e:
        result = {'success':False, 'message':str(e)}
    return json.dumps(result, cls=DecimalEncoder) 

@app.route('/wallet')
def wallet():
    wallet = {'addresses': {}}
    totals = {}
    listaddressgroupings = bitcoin.rpc('listaddressgroupings', [])
    for group in listaddressgroupings:
        for bunch in group:
            address, btc_balance = bunch[:2]
            
            balances = {}
            if config.LIGHT:
                get_address = composer_request('/addresses/'+address)
                if get_address['success']:
                    balances = get_address['message']['balances']
            else:
                get_address = util.get_address(db, address=address)
                balances = get_address['balances']
            
            assets =  {}
            empty = True
            if btc_balance:
                assets['BTC'] = btc_balance
                if 'BTC' in totals.keys(): totals['BTC'] += btc_balance
                else: totals['BTC'] = btc_balance
                empty = False
            for balance in balances:
                asset = balance['asset']
                balance = D(util.devise(db, balance['amount'], balance['asset'], 'output'))
                if balance:
                    if asset in totals.keys(): totals[asset] += balance
                    else: totals[asset] = balance
                    assets[asset] = balance
                    empty = False
            if not empty:
                wallet['addresses'][address] = assets

    wallet['totals'] = totals    
    response.content_type = 'application/json'
    return json.dumps(wallet, cls=DecimalEncoder)


@app.post('/action')
def counterparty_action():

    unsigned = True if getp('unsigned')!=None and getp('unsigned')=="1" else False
    try:     
        if config.MODE=="gui":             
            passphrase = getp('passphrase', None)      
            unlock = wallet_unlock(passphrase)
            if unlock['success']==False:
                raise Exception(unlock['message'])

        action = getp('action')

        if config.LIGHT:
           
            data = {'action': action}
            for param in counterpartyd_params[action]:
                data[param] = getp(param)

            if action in ["btcpay", "cancel"]:
                path = "/"+action+"/"
                if action=="btcpay":
                    path = path+data['order_match_id']
                else:
                    path = path+data['offer_hash']
                path = path+"/source"   
                pubkey_result = composer_request(path)
                if pubkey_result['success']==False:
                    raise Exception("Invalid source address")
                source = pubkey_result['message']
            else:
                source = data['source']

            data['pubkey'] = bitcoin.rpc("dumppubkey", [source])
            result = composer_request('/action', 'POST', data)

        else:
            
            if action=='send':           
                source = getp('source')
                destination = getp('destination')
                asset = getp('asset')  
                quantity = util.devise(db, getp('quantity'), asset, 'input')
                tx_info = send.compose(db, source, destination, asset, quantity)
                result = generate_unsigned_hex(tx_info)       

            elif action=='order':
                source = getp('source')
                give_asset = getp('give_asset')
                get_asset = getp('get_asset')
                fee_fraction_required  = getp('fee_fraction_required', '0')
                fee_fraction_provided = getp('fee_fraction_provided', '0')
                give_quantity = getp('give_quantity', '0')
                get_quantity = getp('get_quantity', '0')
                try:
                    expiration = int(getp('expiration')) 
                except:
                    raise Exception('Invalid expiration')

                # Fee argument is either fee_required or fee_provided, as necessary.
                if give_asset == 'BTC':
                    fee_required = 0
                    fee_fraction_provided = util.devise(db, fee_fraction_provided, 'fraction', 'input')
                    fee_provided = round(D(fee_fraction_provided) * D(give_quantity) * D(config.UNIT))
                    if fee_provided < config.MIN_FEE:
                        raise Exception('Fee provided less than minimum necessary for acceptance in a block.')
                elif get_asset == 'BTC':
                    fee_provided = config.MIN_FEE
                    fee_fraction_required = util.devise(db, fee_fraction_required, 'fraction', 'input')
                    fee_required = round(D(fee_fraction_required) * D(get_quantity) * D(config.UNIT))
                else:
                    fee_required = 0
                    fee_provided = config.MIN_FEE

                give_quantity = util.devise(db, D(give_quantity), give_asset, 'input')
                get_quantity = util.devise(db, D(get_quantity), get_asset, 'input') 
                tx_info = order.compose(db, source, give_asset,
                                        give_quantity, get_asset,
                                        get_quantity, expiration,
                                        fee_required, fee_provided)
                result = generate_unsigned_hex(tx_info) 

            elif action=='btcpay':
                order_match_id = getp('order_match_id')
                tx_info = btcpay.compose(db, order_match_id)
                result = generate_unsigned_hex(tx_info)           

            elif action=='cancel':
                offer_hash = getp('offer_hash')                     
                tx_info = cancel.compose(db, offer_hash)
                result = generate_unsigned_hex(tx_info) 

            elif action=='issuance':
                source = getp('source')
                transfer_destination = getp('transfer_destination')
                asset_name = getp('asset_name')
                divisible = True if getp('divisible')=="1" else False
                quantity = util.devise(db, getp('quantity'), None, 'input', divisible=divisible)

                callable_ = True if getp('callable')=="1" else False
                call_date = getp('call_date')
                call_price = getp('call_price')
                description = getp('description')

                if callable_:
                    if call_date=='':
                        raise Exception('must specify call date of callable asset')
                    if call_price=='':
                        raise Exception('must specify call price of callable asset')
                    call_date = calendar.timegm(dateutil.parser.parse(args.call_date).utctimetuple())
                    call_price = float(args.call_price)
                else:
                    call_date, call_price = 0, 0

                try:
                    quantity = int(quantity)
                except ValueError:
                    raise Exception("Invalid quantity")
                tx_info = issuance.compose(db, source, transfer_destination,
                                           asset_name, quantity, divisible, callable_,
                                           call_date, call_price, description)
                result = generate_unsigned_hex(tx_info) 
            
            elif action=='dividend':
                source = getp('source')
                asset = getp('asset') 
                dividend_asset = getp('dividend_asset') 
                quantity_per_share = util.devise(db, getp('quantity_per_share'), dividend_asset, 'input')          
                tx_info = dividend.compose(db, source, quantity_per_share, asset)
                result = generate_unsigned_hex(tx_info) 

            elif action=='callback':
                source = getp('source')
                asset = getp('asset')
                fraction_per_share = util.devise(db, getp('fraction_per_share'), 'fraction', 'input')
                tx_info = callback.compose(db, source, fraction_per_share, asset)
                result = generate_unsigned_hex(tx_info) 

            elif action=='broadcast':
                source = getp('source')
                text = getp('text')
                value = util.devise(db, getp('value'), 'value', 'input')
                fee_fraction = util.devise(db, getp('fee_fraction'), 'fraction', 'input')
                tx_info = broadcast.compose(db, source, int(time.time()), value, fee_fraction, text)
                result = generate_unsigned_hex(tx_info) 

            elif action=='bet':
                source = getp('source')
                feed_address = getp('feed_address')
                bet_type = int(getp('bet_type'))
                deadline = calendar.timegm(dateutil.parser.parse(getp('deadline')).utctimetuple())
                wager = util.devise(db, getp('wager'), 'XCP', 'input')
                counterwager = util.devise(db, getp('counterwager'), 'XCP', 'input')
                target_value = util.devise(db, getp('target_value'), 'value', 'input')
                leverage = util.devise(db, getp('leverage'), 'leverage', 'input')
                expiration = getp('expiration')
                tx_info = bet.compose(db, source, feed_address,
                                      bet_type, deadline, wager,
                                      counterwager, target_value,
                                      leverage, expiration)
                result = generate_unsigned_hex(tx_info) 

            else:
                result = {'success':False, 'message':'Unknown action.'} 

        if config.MODE=="gui" and result['success']==True and unsigned==False:
            unsigned_tx_hex = result['message']
            tx_hash = bitcoin.transmit(unsigned_tx_hex);
            result['message'] = "Transaction transmited: "+tx_hash

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_tb(exc_traceback, limit=5)
        message = str(e)
        result = {'success':False, 'message':message} 


    response.content_type = 'application/json'
    return json.dumps(result, cls=DecimalEncoder)

@app.route('/<filename:path>')
def send_static(filename):
    return static_file(filename, root=config.GUI_DIR)

def run_server():

    parser = argparse.ArgumentParser(prog='counterpartws', description='Browser GUI for Counterpartyd')
    parser.add_argument('-c', '--composer', dest='composer', action='store_true', help='Run as transactions composer server')
    parser.add_argument('-l', '--light', dest='light', action='store_true', help='Don\'t follow blocks and use composer server')
    parser.add_argument('--bitcoind-rpc-connect', help='the hostname or IP of the bitcoind JSON-RPC server')
    parser.add_argument('--bitcoind-rpc-port', type=int, help='the bitcoind JSON-RPC port to connect to')

    args = parser.parse_args()

    if args.composer:
        config.MODE = "composer"
        config.LIGHT = False
        config.GUI_HOST = config.COMPOSER_HOST
        config.GUI_PORT = config.COMPOSER_PORT
        if args.bitcoind_rpc_connect:
            config.BITCOIND_RPC_CONNECT = args.bitcoind_rpc_connect
        if args.bitcoind_rpc_port:
            config.BITCOIND_RPC_PORT = args.bitcoind_rpc_port
    else:
        config.MODE = "gui"
        if args.light:
            bictoind_infos = bitcoin.rpc("getinfo", [])
            if 'pyrpcwallet' not in bictoind_infos:
                raise Exception("You must have pyrpcwallet running to run counterpartyws in light mode")
            config.LIGHT = True

    write_pid()

    app.run(host=config.GUI_HOST, port=config.GUI_PORT)


if __name__ == '__main__':
    run_server()




